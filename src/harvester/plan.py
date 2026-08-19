"""Нарезка глубины на окна и наполнение очереди заданий.

Обход всей глубины одним окном невозможен и не нужен: окно — это единица
работы, которую можно переделать, если она не удалась. Месяц выбран как
компромисс: у самой крупной картотеки это около 90 страниц, у самой мелкой —
одна, и в обоих случаях потеря окна стоит недорого.

Число ЗАПРОСОВ от размера окна не зависит — страницы всё равно по 25 записей.
Зависит только зернистость возобновления.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import insert

from .config import Settings
from .config import settings as default_settings
from .db.schema import harvest_task
from .directories import cartoteki, courts
from .urls import DateAxis

#: Кассационные суды ОСЮ работают с 01.10.2019 — раньше этой даты дел нет.
CORPUS_START = date(2019, 10, 1)


@dataclass(frozen=True, slots=True)
class Window:
    start: date
    end: date


def month_windows(start: date = CORPUS_START, end: date | None = None) -> list[Window]:
    """Календарные месяцы от `start` до `end` включительно.

    Границы календарные, а не «30 дней»: так окна совпадают с тем, как человек
    говорит о периоде, и повторный проход за март даёт ровно март.
    """
    end = end or date.today()
    if end < start:
        raise ValueError("конец глубины раньше начала")

    windows: list[Window] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        first = date(year, month, 1)
        last = date(year, month, monthrange(year, month)[1])
        windows.append(Window(max(first, start), min(last, end)))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return windows


def fill_queue(
    *,
    settings: Settings | None = None,
    axis: DateAxis = DateAxis.PUBLICATION,
    start: date = CORPUS_START,
    end: date | None = None,
    only_courts: list[str] | None = None,
    only_cartoteki: list[str] | None = None,
) -> tuple[int, int]:
    """Записать задания в очередь. Возвращает (сколько добавлено, сколько уже было).

    Повторный вызов безопасен: окна уникальны по паре суд × картотека × ось
    × границы, и существующие задания не трогаются — иначе перезапуск
    планировщика сбрасывал бы прогресс.
    """
    settings = settings or default_settings
    engine = create_engine(settings.database_url)

    windows = month_windows(start, end)
    rows = [
        {
            "court_domain": court.domain,
            "cartoteka_id": cartoteka.id,
            "axis": axis.value,
            "window_from": window.start,
            "window_to": window.end,
            "status": "pending",
        }
        for court in courts()
        if only_courts is None or court.domain in only_courts
        for cartoteka in cartoteki()
        if only_cartoteki is None or cartoteka.id in only_cartoteki
        for window in windows
    ]

    with engine.begin() as connection:
        before = connection.execute(select(func.count()).select_from(harvest_task)).scalar_one()
        if rows:
            connection.execute(
                insert(harvest_task)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_task_window")
            )
        after = connection.execute(select(func.count()).select_from(harvest_task)).scalar_one()
    engine.dispose()

    added = after - before
    return added, len(rows) - added
