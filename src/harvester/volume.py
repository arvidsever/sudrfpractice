"""Замер объёма картотек: сколько всего дел в каждой паре суд × картотека.

Пока объём известен только оценкой по недельному темпу, планировать обход
нельзя: неизвестно ни сколько это займёт, ни где обход недобрал. Один запрос
без фильтра дат отдаёт счётчик по всей картотеке — сорок запросов превращают
оценку в факт.

Замер идёт через тот же клиент с дросселем, что и обход: правило «любой
запрос к суду — через CourtClient» появилось не просто так.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert

from .client import open_client
from .config import Settings
from .config import settings as default_settings
from .db import store
from .db.schema import cartoteka_volume
from .directories import Cartoteka, Court, cartoteki, courts
from .guards import Verdict, classify
from .http import CourtClient
from .raw import RawRecord, RawStore
from .urls import whole_cartoteka_url

log = logging.getLogger("harvester.volume")


@dataclass(slots=True)
class Measurement:
    court_domain: str
    cartoteka_id: str
    total_cases: int | None
    status: str
    note: str | None = None
    #: Провенанс сохранённой страницы. `None`, если замеряли без хранилища
    #: (тесты) или до суда дело не дошло.
    raw: RawRecord | None = None


def measure_pair(
    client: CourtClient,
    court: Court,
    cartoteka: Cartoteka,
    raw_store: RawStore | None = None,
) -> Measurement:
    url = whole_cartoteka_url(court, cartoteka)
    try:
        response = client.get_passing_captcha(url)
    except Exception as exc:  # noqa: BLE001 — один суд не должен ронять замер
        return Measurement(
            court.domain, cartoteka.id, None, "failed", f"{type(exc).__name__}: {exc}"
        )

    # Сырьё замера хранится по той же причине, что и сырьё обхода: объяснять
    # непонятный ответ надо, не обращаясь к суду второй раз. Вердикт
    # `throttled` ставится по фразе, которую ищут по всей странице, — заглушка
    # и обычная страница с той же фразой в вёрстке по одному лишь статусу
    # неразличимы, а разбираться постфактум без сохранённых байтов нечем.
    raw = (
        raw_store.save(
            response.content,
            url=url,
            court_domain=court.domain,
            http_status=response.status_code,
            content_kind="volume",
        )
        if raw_store is not None
        else None
    )

    state = classify(response.text)
    if state.verdict is Verdict.LISTING and state.total is not None:
        return Measurement(court.domain, cartoteka.id, state.total, "measured", raw=raw)
    if state.verdict is Verdict.THROTTLED:
        return Measurement(
            court.domain, cartoteka.id, None, "throttled", "суд придержал адрес", raw=raw
        )
    if state.verdict is Verdict.NO_DATA:
        # Пустая картотека и кривой запрос по тексту неотличимы (§5 грамматики),
        # поэтому это не «нуль дел», а «нечего засчитывать».
        return Measurement(
            court.domain, cartoteka.id, None, "empty", "выдача пуста либо запрос кривой", raw=raw
        )
    return Measurement(court.domain, cartoteka.id, None, "failed", state.verdict.value, raw=raw)


def measure_all(
    *,
    settings: Settings | None = None,
    only_courts: list[str] | None = None,
    only_cartoteki: list[str] | None = None,
    bulk: bool = True,
    skip_measured: bool = True,
) -> list[Measurement]:
    """Обойти пары суд × картотека и записать счётчики."""
    settings = settings or default_settings
    engine = create_engine(settings.database_url)

    targets = [
        (court, cartoteka)
        for court in courts()
        if only_courts is None or court.domain in only_courts
        for cartoteka in cartoteki()
        if only_cartoteki is None or cartoteka.id in only_cartoteki
    ]

    if skip_measured:
        from sqlalchemy import select

        with engine.connect() as connection:
            done = {
                (row.court_domain, row.cartoteka_id)
                for row in connection.execute(
                    select(cartoteka_volume.c.court_domain, cartoteka_volume.c.cartoteka_id).where(
                        cartoteka_volume.c.status == "measured"
                    )
                )
            }
        targets = [t for t in targets if (t[0].domain, t[1].id) not in done]

    raw_store = RawStore(settings.raw_root)

    results: list[Measurement] = []
    for court, cartoteka in targets:
        with open_client(court, cartoteka, settings=settings, bulk=bulk) as client:
            measurement = measure_pair(client, court, cartoteka, raw_store)
            results.append(measurement)
            if measurement.raw is not None:
                with engine.begin() as connection:
                    store.save_raw_page(connection, measurement.raw)
            log.info(
                "%s/%s: %s %s",
                measurement.court_domain,
                measurement.cartoteka_id,
                measurement.status,
                measurement.total_cases if measurement.total_cases is not None else "",
            )

            with engine.begin() as connection:
                statement = insert(cartoteka_volume).values(
                    court_domain=measurement.court_domain,
                    cartoteka_id=measurement.cartoteka_id,
                    total_cases=measurement.total_cases,
                    status=measurement.status,
                    note=measurement.note,
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["court_domain", "cartoteka_id"],
                        set_={
                            "total_cases": statement.excluded.total_cases,
                            "status": statement.excluded.status,
                            "note": statement.excluded.note,
                            "measured_at": statement.excluded.measured_at,
                        },
                    )
                )

            if measurement.status == "throttled":
                log.warning("%s придержал адрес — замер по нему прерван", court.domain)

    engine.dispose()
    return results
