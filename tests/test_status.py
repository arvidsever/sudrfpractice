"""Сводка состояния сбора.

Главное в ней — оценка остатка, и она уже один раз соврала. 20.08.2026
прямая экстраполяция по закрытым окнам обещала полтора суток вместо шести:
к тому часу был закрыт почти весь Военный суд, самый маленький из десяти,
и средний размер окна получился втрое меньше настоящего. Поэтому остаток
считается по строкам, а не по окнам, и этот тест держит именно это.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine, update

from harvester.db.schema import harvest_task
from harvester.plan import fill_queue
from harvester.status import Progress, _percent, _plural, _thousands, collect, render

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _progress(**overrides) -> Progress:
    base = dict(
        windows_done=917,
        windows_total=6640,
        windows_empty_confirmed=0,
        windows_empty_unconfirmed=0,
        rows_collected=163_505,
        rows_expected=2_747_867,
        rate_per_hour=1470.0,
        last_window_at=NOW - timedelta(minutes=2),
        complete_today=831,
        throttled_today=12,
        failed_today=0,
    )
    base.update(overrides)
    return Progress(**base)


def test_estimate_counts_rows_not_windows() -> None:
    """Окна разного размера, и доля закрытых врёт тем сильнее, чем
    неравномернее суды."""
    progress = _progress()

    by_rows = progress.hours_left
    assert by_rows is not None

    # Оценка «по окнам» для сравнения: 917 из 6 640 за то же время.
    windows_left = progress.windows_total - progress.windows_done
    by_windows = windows_left / (progress.windows_done / 1)  # часов, если бы окна были равны
    assert by_rows > by_windows, "оценка по строкам обязана быть осторожнее наивной оценки по окнам"
    assert 100 < by_rows < 200, f"ожидались примерно шесть суток, вышло {by_rows / 24:.1f}"


def test_no_rate_means_no_promise() -> None:
    """Не с чего считать — лучше молчать, чем делить на ноль."""
    assert _progress(rate_per_hour=None).hours_left is None


def test_closed_queue_promises_nothing() -> None:
    """Очередь закрыта — «осталось» отвечать не на что.

    В базе дел вдвое меньше, чем строк в двух осях, потому что обе оси
    ложатся одним делом. Пока остаток считался от этой разницы, закрытая
    очередь обещала четыре тысячи суток сбора.
    """
    assert _progress(windows_done=6640, rows_collected=2_748_430).hours_left is None


def test_silent_run_is_called_out() -> None:
    """Прогон, не закрывший ни одного окна четверть часа, — повод посмотреть.

    Молчание выглядит точно так же, как работа: очередь не пустеет ни в том,
    ни в другом случае. 20.08 девять судов простояли всю ночь именно так.
    """
    working = render(_progress(), [], now=NOW)
    silent = render(_progress(last_window_at=NOW - timedelta(hours=3)), [], now=NOW)

    assert "идёт" in working
    assert "МОЛЧИТ" in silent


def test_failures_are_not_buried() -> None:
    """Неудачи должны звать смотреть, а не теряться строкой в сводке."""
    assert "harvest_run" in render(_progress(failed_today=1086), [], now=NOW)
    assert "harvest_run" not in render(_progress(failed_today=0), [], now=NOW)


def test_empty_window_closes_only_with_a_control(db_settings) -> None:
    """Пустая выдача — подозрение, пока рядом нет окна ТОЙ ЖЕ формы с делами.

    Портал отвечает `200 OK` и на кривой запрос, поэтому «дел нет»
    и «спросили не то» по странице неразличимы. Контроль на такое окно
    не надо запрашивать заново, если соседний месяц той же пары
    суд × картотека × ось уже собран с делами: форма запроса доказана.
    Без такого соседа окно обязано остаться незакрытым.
    """
    fill_queue(
        settings=db_settings,
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        only_courts=["5kas.sudrf.ru"],
        only_cartoteki=["g3"],
    )
    engine = create_engine(db_settings.database_url)
    with engine.connect() as connection:
        ids = [row.id for row in connection.execute(harvest_task.select().order_by("id"))]
    assert len(ids) >= 3, "нарезка должна дать хотя бы три окна"

    def _set(task_id: int, **values) -> None:
        with engine.begin() as connection:
            connection.execute(
                update(harvest_task).where(harvest_task.c.id == task_id).values(**values)
            )

    # Первое окно пусто, контроля пока нет.
    _set(ids[0], status="empty", cases_found=0)
    assert collect(engine).windows_empty_unconfirmed == 1
    assert collect(engine).windows_empty_confirmed == 0
    assert collect(engine).windows_done == 0

    # Соседнее окно той же пары отдало дела — форма запроса доказана.
    _set(ids[1], status="done", cases_found=42)
    progress = collect(engine)
    assert progress.windows_empty_unconfirmed == 0
    assert progress.windows_empty_confirmed == 1
    assert progress.windows_done == 2, "подтверждённая пустота — закрытое окно"

    assert "пусто, контроль есть" in render(progress, [], now=NOW)
    engine.dispose()


def test_numbers_are_readable() -> None:
    assert _thousands(5_303_383) == "5 303 383"
    assert _percent(0.1457) == "14,6 %"
    assert (
        _plural(1, "запрос", "запроса", "запросов"),
        _plural(2, "запрос", "запроса", "запросов"),
        _plural(5, "запрос", "запроса", "запросов"),
        _plural(11, "запрос", "запроса", "запросов"),
        _plural(1847, "запрос", "запроса", "запросов"),
    ) == ("запрос", "запроса", "запросов", "запросов", "запросов")
