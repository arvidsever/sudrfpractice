"""Очередь заданий: захват, отметка, возврат после обрыва.

Главное свойство — возобновляемость. Прогон прерывают: Ctrl+C, обрыв связи,
перезагрузка. После любого из них следующий запуск обязан продолжить с того
же окна, не начав сначала и не потеряв окно молча.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, select

from harvester import queue as task_queue
from harvester.db.schema import harvest_task
from harvester.plan import fill_queue

WINDOW = (date(2026, 1, 1), date(2026, 3, 31))


def _engine(db_settings):
    return create_engine(db_settings.database_url)


def _seed(db_settings, courts=("5kas.sudrf.ru",), cartoteki=("g3",)):
    fill_queue(
        settings=db_settings,
        start=WINDOW[0],
        end=WINDOW[1],
        only_courts=list(courts),
        only_cartoteki=list(cartoteki),
    )
    return _engine(db_settings)


def test_claim_marks_running_and_counts_attempts(db_settings) -> None:
    """`running` ставится СРАЗУ, а не после успеха: прерванный прогон
    обязан оставить видимый след, а не исчезнуть."""
    engine = _seed(db_settings)
    task = task_queue.claim(engine)

    assert task is not None
    assert task.attempts == 1

    with engine.connect() as connection:
        status = connection.execute(
            select(harvest_task.c.status).where(harvest_task.c.id == task.id)
        ).scalar_one()
    engine.dispose()
    assert status == "running"


def test_two_claims_never_collide(db_settings) -> None:
    """Два процесса могут работать одновременно — одно окно дважды
    не достанется."""
    engine = _seed(db_settings)
    first, second = task_queue.claim(engine), task_queue.claim(engine)
    engine.dispose()

    assert first is not None and second is not None
    assert first.id != second.id


def test_newest_windows_go_first(db_settings) -> None:
    """Свежая практика нужнее старой, поэтому очередь идёт от новых окон."""
    engine = _seed(db_settings)
    task = task_queue.claim(engine)
    engine.dispose()
    assert task.window_from == date(2026, 3, 1)


def test_court_on_pause_is_skipped_not_broken(db_settings) -> None:
    """Пауза — причина заняться другим судом, а не остановиться."""
    engine = _seed(db_settings, courts=("5kas.sudrf.ru", "9kas.sudrf.ru"))
    task = task_queue.claim(engine, skip={"5kas.sudrf.ru"})
    engine.dispose()

    assert task is not None
    assert task.court_domain == "9kas.sudrf.ru"


def test_interrupted_run_returns_to_the_queue(db_settings) -> None:
    """`running` живёт только пока живёт прогон. Без уборки очередь
    медленно опустела бы в никуда."""
    engine = _seed(db_settings)
    task_queue.claim(engine)
    task_queue.claim(engine)

    released = task_queue.release_stale(engine)
    assert released == 2

    with engine.connect() as connection:
        statuses = set(connection.execute(select(harvest_task.c.status)).scalars())
    engine.dispose()
    assert statuses == {"pending"}


def test_hopeless_window_stops_being_taken(db_settings) -> None:
    """Окно, падающее раз за разом, не должно крутиться в очереди вечно."""
    engine = _seed(db_settings, cartoteki=("g3",))
    for _ in range(task_queue.MAX_ATTEMPTS):
        task = task_queue.claim(engine, courts=["5kas.sudrf.ru"])
        task_queue.complete(engine, task, status="failed", error="проверка")

    # Взять можно только два оставшихся окна, а не это.
    seen = set()
    while (task := task_queue.claim(engine, courts=["5kas.sudrf.ru"])) is not None:
        seen.add(task.window_from)
    engine.dispose()
    assert date(2026, 3, 1) not in seen


def test_done_window_is_not_taken_again(db_settings) -> None:
    engine = _seed(db_settings)
    task = task_queue.claim(engine)
    task_queue.complete(engine, task, status="done", cases_found=42)

    remaining = []
    while (nxt := task_queue.claim(engine)) is not None:
        remaining.append(nxt.id)
    engine.dispose()
    assert task.id not in remaining


def test_summary_counts_by_status(db_settings) -> None:
    engine = _seed(db_settings)
    task = task_queue.claim(engine)
    task_queue.complete(engine, task, status="done", cases_found=1)
    counts = dict(task_queue.summary(engine))
    engine.dispose()

    assert counts["done"] == 1
    assert counts["pending"] == 2


def test_end_of_night_returns_the_window_untouched(db_settings, monkeypatch) -> None:
    """Ночное окно кончилось — это не сбой окна.

    Без отдельной обработки прогон на рассвете сжёг бы попытки у десятков
    окон подряд, и часть из них выбыла бы из очереди навсегда.
    """
    from sqlalchemy import create_engine, select

    from harvester import run as run_module
    from harvester.db.schema import harvest_task
    from harvester.http import OutsideCollectionWindow

    fill_queue(
        settings=db_settings,
        start=WINDOW[0],
        end=WINDOW[1],
        only_courts=["5kas.sudrf.ru"],
        only_cartoteki=["g3"],
    )

    def dawn(*args, **kwargs):
        raise OutsideCollectionWindow("массовый обход разрешён только с 1:00 до 7:00")

    monkeypatch.setattr(run_module, "harvest_listing", dawn)
    run_module.run_queue(settings=db_settings, only_courts=["5kas.sudrf.ru"])

    engine = create_engine(db_settings.database_url)
    with engine.connect() as connection:
        statuses = list(connection.execute(select(harvest_task.c.status)).scalars())
    engine.dispose()

    assert set(statuses) == {"pending"}, "окно должно вернуться в очередь нетронутым"
