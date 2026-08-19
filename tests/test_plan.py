"""Нарезка глубины на окна и наполнение очереди."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select

from harvester.db.schema import harvest_task
from harvester.plan import CORPUS_START, fill_queue, month_windows


def test_windows_are_calendar_months() -> None:
    """Границы календарные, а не «30 дней»: повторный проход за март
    даёт ровно март, и окна совпадают с тем, как о периоде говорят."""
    windows = month_windows(date(2026, 1, 1), date(2026, 3, 31))

    assert len(windows) == 3
    assert (windows[0].start, windows[0].end) == (date(2026, 1, 1), date(2026, 1, 31))
    assert (windows[1].start, windows[1].end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert (windows[2].start, windows[2].end) == (date(2026, 3, 1), date(2026, 3, 31))


def test_partial_months_are_clipped_not_dropped() -> None:
    """Глубина редко начинается первого числа: КСОЮ работают с 01.10.2019,
    а конец глубины — сегодняшний день посреди месяца."""
    windows = month_windows(date(2026, 1, 15), date(2026, 2, 10))

    assert (windows[0].start, windows[0].end) == (date(2026, 1, 15), date(2026, 1, 31))
    assert (windows[-1].start, windows[-1].end) == (date(2026, 2, 1), date(2026, 2, 10))


def test_leap_year_february() -> None:
    assert month_windows(date(2028, 2, 1), date(2028, 2, 29))[0].end == date(2028, 2, 29)


def test_corpus_starts_when_the_courts_did() -> None:
    """Раньше 01.10.2019 кассационных судов ОСЮ не существовало."""
    assert date(2019, 10, 1) == CORPUS_START
    assert month_windows(CORPUS_START, date(2019, 12, 31))[0].start == CORPUS_START


def test_end_before_start_is_an_error() -> None:
    with pytest.raises(ValueError):
        month_windows(date(2026, 3, 1), date(2026, 1, 1))


def test_queue_is_filled_once(db_settings) -> None:
    """Повторный запуск планировщика не трогает существующие задания:
    иначе он сбрасывал бы прогресс обхода."""
    added, existed = fill_queue(
        settings=db_settings,
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        only_courts=["5kas.sudrf.ru"],
        only_cartoteki=["g3"],
    )
    assert (added, existed) == (3, 0)

    added, existed = fill_queue(
        settings=db_settings,
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        only_courts=["5kas.sudrf.ru"],
        only_cartoteki=["g3"],
    )
    assert (added, existed) == (0, 3)

    engine = create_engine(db_settings.database_url)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(harvest_task)).scalar_one() == 3
        statuses = connection.execute(select(harvest_task.c.status).distinct()).scalars().all()
    engine.dispose()
    assert statuses == ["pending"]


def test_queue_covers_every_pair_and_window(db_settings) -> None:
    added, _ = fill_queue(
        settings=db_settings,
        start=date(2026, 1, 1),
        end=date(2026, 2, 28),
        only_courts=["5kas.sudrf.ru", "9kas.sudrf.ru"],
    )
    # 2 суда × 4 картотеки × 2 месяца
    assert added == 16
