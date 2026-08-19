"""Очередь заданий: взять следующее окно, выполнить, отметить.

Пока обход запускался руками, «что уже собрано» жило в голове. Очередь
переносит это в базу, и от неё требуется ровно одно свойство —
**возобновляемость**. Прогон убивают, сеть рвётся, суд просит отступить;
после любого из этого следующий запуск обязан продолжить с того же места,
а не начать сначала и не пропустить окно молча.

Отсюда три решения:

* задание захватывается атомарно, через `FOR UPDATE SKIP LOCKED`. Два
  процесса могут работать одновременно и не возьмут одно окно дважды;
* захваченное задание помечается `running` сразу, а не после успеха.
  Прерванный прогон оставляет видимый след, а не исчезает;
* суд на паузе пропускается, и берётся окно другого суда. Пауза —
  причина заняться другим, а не остановиться.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Engine, func, select, text, update

from .db.schema import harvest_task

log = logging.getLogger("harvester.queue")

#: После скольких неудач окно больше не берётся автоматически.
MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class Task:
    id: int
    court_domain: str
    cartoteka_id: str
    axis: str
    window_from: date
    window_to: date
    attempts: int


def claim(engine: Engine, *, courts: list[str] | None = None, skip: set[str] | None = None):
    """Захватить следующее незакрытое окно. `None` — брать нечего.

    `skip` — суды на паузе: их окна не берём, но и не портим, они просто
    достанутся следующему заходу.
    """
    conditions = ["status IN ('pending', 'failed', 'throttled')", "attempts < :max_attempts"]
    params: dict[str, object] = {"max_attempts": MAX_ATTEMPTS}

    if courts:
        conditions.append("court_domain = ANY(:courts)")
        params["courts"] = list(courts)
    if skip:
        conditions.append("court_domain <> ALL(:skip)")
        params["skip"] = list(skip)

    statement = text(
        f"""
        UPDATE harvest_task SET status = 'running', attempts = attempts + 1,
                                updated_at = now()
        WHERE id = (
            SELECT id FROM harvest_task
            WHERE {" AND ".join(conditions)}
            ORDER BY window_from DESC, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, court_domain, cartoteka_id, axis, window_from, window_to, attempts
        """
    )

    with engine.begin() as connection:
        row = connection.execute(statement, params).one_or_none()
    return None if row is None else Task(*row)


def complete(
    engine: Engine,
    task: Task,
    *,
    status: str,
    cases_found: int | None = None,
    run_id: int | None = None,
    error: str | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(harvest_task)
            .where(harvest_task.c.id == task.id)
            .values(
                status=status,
                cases_found=cases_found,
                run_id=run_id,
                last_error=error,
                updated_at=func.now(),
            )
        )


def release_stale(engine: Engine) -> int:
    """Вернуть в очередь окна, помеченные `running` прошлым прогоном.

    Процесс, помеченный `running`, живёт только пока живёт прогон. Если
    его убили, задание останется висеть — и без этой уборки очередь
    медленно опустеет в никуда.
    """
    with engine.begin() as connection:
        result = connection.execute(
            update(harvest_task)
            .where(harvest_task.c.status == "running")
            .values(status="pending", updated_at=func.now())
        )
    return result.rowcount


def summary(engine: Engine) -> list[tuple[str, int]]:
    with engine.connect() as connection:
        return [
            (row.status, row.count)
            for row in connection.execute(
                select(harvest_task.c.status, func.count().label("count"))
                .group_by(harvest_task.c.status)
                .order_by(func.count().desc())
            )
        ]
