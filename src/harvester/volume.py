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
from datetime import date, timedelta

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

#: Ниже этого куск дробить не будем: если суд не считает даже месяц,
#: дело уже не в цене запроса. Месяц выбран не наугад — именно такими
#: окнами идёт индекс, и `3kas/g3` на месяце отвечает (2 000 дел за июнь
#: 2026), а на трети глубины уже нет.
_MIN_SPAN_DAYS = 31

#: Сколько запросов позволено второй попытке. Дробление адаптивное,
#: и без потолка кривой ответ увёл бы его в долгий обход.
_MAX_PROBES = 32


def measure_pair(
    client: CourtClient,
    court: Court,
    cartoteka: Cartoteka,
    raw_store: RawStore | None = None,
    today: date | None = None,
) -> Measurement:
    """Замерить пару. Сперва счётчиком по всей картотеке, а если суд
    на нём споткнулся — суммой счётчиков по нескольким кускам глубины.

    Запрос без фильтра дат — самый дорогой из возможных: суд считает всю
    картотеку разом. У больших картотек сервер этого не выдерживает
    и отвечает «Информация временно недоступна» — той же страницей, какой
    он просит отступить, когда придерживает адрес. Отличить одно от другого
    по странице нельзя, а вот обойти — можно, если спрашивать по частям.

    Окно «01.10.2019 — сегодня» тут не помогло бы: это и есть вся глубина,
    та же работа для сервера, только с датами в параметрах. Поэтому глубина
    делится пополам, а кусок, который не дался, делится дальше — пока суд
    не сосчитает или пока кусок не сожмётся до месяца.

    Складывать их законно именно по оси ПОСТУПЛЕНИЯ: дата поступления
    у дела одна, куски не пересекаются и ничего не теряют на стыках.
    По оси публикации сумма была бы неверной — у дела может быть несколько
    опубликованных актов.

    Числа всё же не тождественны: без фильтра счётчик берёт всё, сумма
    кусков — только попавшее в глубину с 01.10.2019. Поэтому результат
    помечается в примечании, чтобы пара не выбивалась из ряда соседей молча.
    """
    url = whole_cartoteka_url(court, cartoteka)
    try:
        # Пауза по этому ответу не взводится: пока не проверен облегчённый
        # запрос, «временно недоступна» здесь ещё не значит «отойди».
        # Иначе пауза встаёт раньше второй попытки и та упирается в неё же.
        response = client.get_passing_captcha(url, arm_back_off=False)
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
        return _measure_by_chunks(client, court, cartoteka, raw_store, today=today, first_raw=raw)
    if state.verdict is Verdict.NO_DATA:
        # Пустая картотека и кривой запрос по тексту неотличимы (§5 грамматики),
        # поэтому это не «нуль дел», а «нечего засчитывать».
        return Measurement(
            court.domain, cartoteka.id, None, "empty", "выдача пуста либо запрос кривой", raw=raw
        )
    return Measurement(court.domain, cartoteka.id, None, "failed", state.verdict.value, raw=raw)


def split_by_years(start: date, end: date) -> list[tuple[date, date]]:
    """Порезать глубину по календарным годам.

    Год — разумное первое приближение: он заведомо легче всей картотеки
    и заведомо тяжелее месяца, а границы читаются человеком. Крайние годы
    неполные, и это правильно: глубина начинается 01.10.2019.
    """
    chunks = []
    year_from = start
    while year_from <= end:
        year_to = min(date(year_from.year, 12, 31), end)
        chunks.append((year_from, year_to))
        year_from = date(year_from.year + 1, 1, 1)
    return chunks


def split_in_two(start: date, end: date) -> list[tuple[date, date]]:
    """Разделить отрезок пополам встык, без нахлёста и без дыры."""
    days = (end - start).days
    if days < 1:
        return [(start, end)]
    middle = start + timedelta(days=days // 2)
    return [(start, middle), (middle + timedelta(days=1), end)]


def _measure_by_chunks(
    client: CourtClient,
    court: Court,
    cartoteka: Cartoteka,
    raw_store: RawStore | None,
    *,
    today: date | None,
    first_raw: RawRecord | None,
) -> Measurement:
    """Вторая попытка: дробить глубину, пока суд не сможет сосчитать.

    На сколько частей резать — заранее неизвестно: у 1 КСОЮ счётчик
    по всей гражданской картотеке (236 445 дел) отдаётся сразу, а у 3 КСОЮ
    не отдаётся даже треть глубины. Дело не в размере картотеки, а в том,
    сколько тянет конкретный сервер. Поэтому глубина сперва режется
    по календарным годам, а год, который не дался, делится пополам,
    и так до месяца — ниже дробить бессмысленно, месяц 3 КСОЮ считает.

    Складывать куски законно именно по оси ПОСТУПЛЕНИЯ: дата поступления
    у дела одна, куски встык и ничего не теряют на стыках. По оси
    публикации сумма была бы неверной — у дела может быть несколько
    опубликованных актов.
    """
    pending = list(reversed(split_by_years(CORPUS_START, today or date.today())))
    total, probes, deepest = 0, 0, 0
    raw = first_raw

    while pending:
        chunk_from, chunk_to = pending.pop()
        window = f"{chunk_from:%d.%m.%Y}\u2013{chunk_to:%d.%m.%Y}"
        span = (chunk_to - chunk_from).days + 1
        deepest = max(deepest, span)

        if probes >= _MAX_PROBES:
            return Measurement(
                court.domain,
                cartoteka.id,
                None,
                "throttled",
                f"дробление упёрлось в потолок в {_MAX_PROBES} запросов, "
                f"не сосчитан кусок {window}",
                raw=raw,
            )
        probes += 1

        try:
            response = client.get_passing_captcha(url_for(court, cartoteka, chunk_from, chunk_to))
        except Exception as exc:  # noqa: BLE001 — один суд не должен ронять замер
            return Measurement(
                court.domain,
                cartoteka.id,
                None,
                "throttled",
                f"счётчик ко всей картотеке не дался; кусок {window} — {type(exc).__name__}: {exc}",
                raw=raw,
            )

        if raw_store is not None:
            raw = raw_store.save(
                response.content,
                url=url_for(court, cartoteka, chunk_from, chunk_to),
                court_domain=court.domain,
                http_status=response.status_code,
                content_kind="volume",
            )

        state = classify(response.text)
        if state.verdict is Verdict.LISTING and state.total is not None:
            total += state.total
            continue

        if state.verdict is Verdict.THROTTLED and span > _MIN_SPAN_DAYS:
            pending.extend(reversed(split_in_two(chunk_from, chunk_to)))
            continue

        if state.verdict is Verdict.THROTTLED:
            # Не дался даже месяц — значит дело уже не в цене запроса,
            # и просьбу отойти надо исполнить.
            client.back_off(court.domain, client.settings.cooldown_seconds, "суд придержал адрес")
        return Measurement(
            court.domain,
            cartoteka.id,
            None,
            "throttled",
            f"счётчик ко всей картотеке не дался; кусок {window} — {state.verdict.value}",
            raw=raw,
        )

    return Measurement(
        court.domain,
        cartoteka.id,
        total,
        "measured",
        f"счётчик ко всей картотеке суду не дался; сумма {probes} кусков "
        f"по оси поступления с {CORPUS_START:%d.%m.%Y}",
        raw=raw,
    )


def url_for(court: Court, cartoteka: Cartoteka, date_from: date, date_to: date) -> str:
    """Счётчик за окно по оси поступления — она одна годится для сложения."""
    return listing_url(court, cartoteka, DateAxis.ENTRY, date_from, date_to)


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
