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
from datetime import date

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
from .urls import DateAxis, listing_url, whole_cartoteka_url

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


#: Начало глубины: кассационные суды ОСЮ работают с 01.10.2019.
#: Дублирует `plan.CORPUS_START`; импортировать оттуда значило бы завести
#: зависимость замера от планировщика ради одной константы.
CORPUS_START = date(2019, 10, 1)


def measure_pair(
    client: CourtClient,
    court: Court,
    cartoteka: Cartoteka,
    raw_store: RawStore | None = None,
    today: date | None = None,
) -> Measurement:
    """Замерить пару. Сперва счётчиком по всей картотеке, а если суд
    на нём споткнулся — тем же счётчиком, но с фильтром дат на всю глубину.

    Запрос без фильтра дат — самый дорогой из возможных: суд считает всю
    картотеку разом. У больших картотек сервер этого не выдерживает
    и отвечает «Информация временно недоступна» — той же страницей, какой
    он просит отступить, когда придерживает адрес. Отличить одно от другого
    по странице нельзя, а вот проверить можно: тот же запрос с окном дат
    суду по силам. Так закрылась пара `3kas/g3`, три раза не дававшаяся
    без фильтра.

    Числа при этом слегка разные: без фильтра счётчик берёт всё, с окном —
    только попавшее в него. Поэтому вторая попытка идёт по оси ПОСТУПЛЕНИЯ,
    которая охватывает и нерассмотренные дела, и её результат помечается
    в примечании — чтобы никто потом не гадал, отчего пара выбивается.
    """
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
        return _measure_with_dates(client, court, cartoteka, raw_store, today=today, first_raw=raw)
    if state.verdict is Verdict.NO_DATA:
        # Пустая картотека и кривой запрос по тексту неотличимы (§5 грамматики),
        # поэтому это не «нуль дел», а «нечего засчитывать».
        return Measurement(
            court.domain, cartoteka.id, None, "empty", "выдача пуста либо запрос кривой", raw=raw
        )
    return Measurement(court.domain, cartoteka.id, None, "failed", state.verdict.value, raw=raw)


def _measure_with_dates(
    client: CourtClient,
    court: Court,
    cartoteka: Cartoteka,
    raw_store: RawStore | None,
    *,
    today: date | None,
    first_raw: RawRecord | None,
) -> Measurement:
    """Вторая попытка: тот же счётчик, но с окном дат на всю глубину."""
    window_to = today or date.today()
    url = listing_url(court, cartoteka, DateAxis.ENTRY, CORPUS_START, window_to)
    window = f"{CORPUS_START:%d.%m.%Y}–{window_to:%d.%m.%Y}"

    try:
        response = client.get_passing_captcha(url)
    except Exception as exc:  # noqa: BLE001 — один суд не должен ронять замер
        return Measurement(
            court.domain,
            cartoteka.id,
            None,
            "throttled",
            f"без фильтра дат суд не ответил; с окном {window} — {type(exc).__name__}: {exc}",
            raw=first_raw,
        )

    raw = (
        raw_store.save(
            response.content,
            url=url,
            court_domain=court.domain,
            http_status=response.status_code,
            content_kind="volume",
        )
        if raw_store is not None
        else first_raw
    )

    state = classify(response.text)
    if state.verdict is Verdict.LISTING and state.total is not None:
        return Measurement(
            court.domain,
            cartoteka.id,
            state.total,
            "measured",
            f"счётчик ко всей картотеке суду не дался; замерено окном {window} по оси поступления",
            raw=raw,
        )
    return Measurement(
        court.domain,
        cartoteka.id,
        None,
        "throttled",
        f"«временно недоступна» и без фильтра дат, и окном {window} — {state.verdict.value}",
        raw=raw,
    )


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
