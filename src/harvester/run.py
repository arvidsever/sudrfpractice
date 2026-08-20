"""Прогон очереди: берёт окна и обходит их, пока есть что брать.

Один поток на суд. Не ради скорости внутри суда — там всё равно дроссель, —
а потому что суды независимы: пока один держит паузу, остальные работают.

Прогон рассчитан на то, что его прервут. Ctrl+C, обрыв связи, перезагрузка —
после любого из них следующий запуск продолжит с того же окна: незакрытые
задания возвращаются в очередь в начале прогона.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from sqlalchemy import create_engine

from . import queue as task_queue
from .config import Settings
from .config import settings as default_settings
from .directories import cartoteka as find_cartoteka
from .directories import court as find_court
from .directories import courts
from .harvest import harvest_listing
from .http import _COOLDOWNS, CourtOnCooldown, DailyCapReached, OutsideCollectionWindow
from .urls import DateAxis

log = logging.getLogger("harvester.run")

#: На какие статусы обхода переводится задание.
_TASK_STATUS = {
    "complete": "done",
    "pilot": "done",
    "empty": "empty",
    "short": "failed",
    "throttled": "throttled",
    "failed": "failed",
}


@dataclass
class RunTotals:
    windows: int = 0
    cases: int = 0
    acts: int = 0
    failed: int = 0
    throttled: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(
        self, *, cases: int = 0, acts: int = 0, failed: bool = False, throttled: bool = False
    ) -> None:
        with self.lock:
            self.windows += 1
            self.cases += cases
            self.acts += acts
            self.failed += int(failed)
            self.throttled += int(throttled)


def _cooling_courts() -> set[str]:
    import time

    now = time.monotonic()
    return {host for host, until in _COOLDOWNS.items() if until > now}


def _cooldown_left(domain: str) -> float:
    """Сколько секунд ещё нельзя трогать этот суд."""
    import time

    return max(0.0, _COOLDOWNS.get(domain, 0.0) - time.monotonic())


def _wait_out_cooldown(domain: str, stop: threading.Event) -> bool:
    """Дождаться конца паузы у суда. `False` — ждать больше нечего, уходим.

    Раньше поток на паузе просто заканчивался: ночной прогон всё равно
    умирал к утру, и следующий запуск начинал с чистого листа. При
    круглосуточной работе процесс живёт сутками, и такой уход означал бы,
    что суд, однажды попросивший отступить, больше не собирается никогда.
    20.08.2026 так и вышло: к семи утра из десяти судов работал один,
    а у девяти оставалось по шестьсот несобранных окон.
    """
    while not stop.is_set():
        left = _cooldown_left(domain)
        if left <= 0:
            return True
        log.info("%s: пауза ещё %.0f мин, поток ждёт", domain, left / 60)
        stop.wait(min(left, 60.0))
    return False


def _work_one_court(
    engine,
    domain: str,
    totals: RunTotals,
    settings: Settings,
    bulk: bool,
    limit: int | None,
    stop: threading.Event,
) -> None:
    done = 0
    while not stop.is_set() and (limit is None or done < limit):
        if not _wait_out_cooldown(domain, stop):
            return

        task = task_queue.claim(engine, courts=[domain])
        if task is None:
            return

        try:
            result = harvest_listing(
                find_court(task.court_domain),
                find_cartoteka(task.cartoteka_id),
                DateAxis(task.axis),
                task.window_from,
                task.window_to,
                settings=settings,
                bulk=bulk,
            )
        except OutsideCollectionWindow as exc:
            # Ночное окно кончилось. Это не сбой окна: возвращаем задание
            # нетронутым — вместе с попыткой, которую списал `claim`. Иначе
            # на рассвете прогон сжигал бы попытку у окна на каждый суд,
            # и однажды окно выбыло бы из очереди навсегда.
            task_queue.complete(engine, task, status="pending", refund=True)
            log.info("%s: %s — прогон останавливается до следующей ночи", domain, exc)
            stop.set()
            return
        except DailyCapReached as exc:
            # Потолок на сегодня выбран. Окно не виновато и не пробовано:
            # возвращаем вместе с попыткой и уходим до завтра. Без этой
            # ветки оно попало бы в общий `except` и легло бы как `failed`,
            # а следом за ним и все остальные окна этого суда.
            task_queue.complete(engine, task, status="pending", refund=True)
            log.info("%s: %s — суд отложен до завтра", domain, exc)
            return  # потолок снимет полночь, а её ждёт уже launchd
        except CourtOnCooldown as exc:
            # Не вина окна: суд просто попросил отступить. Возвращаем задание
            # в очередь, не тратя попытку, и ждём — а не уходим насовсем.
            # Пока прогон был ночным, уйти было не жалко: процесс всё равно
            # умирал к утру. Круглосуточный процесс живёт сутками, и суд,
            # раз попросивший паузу, больше не собирался бы никогда:
            # 20.08 из десяти судов к утру работал один.
            task_queue.complete(engine, task, status="throttled", error=str(exc), refund=True)
            totals.add(throttled=True)
            if _cooldown_left(domain) <= 0:
                # Пауза не проставлена — ждать нечего и незачем: без этой
                # проверки поток брал бы то же окно по кругу. Статус
                # `throttled` очередь снова выдаёт, так что круг был бы вечным.
                log.info("%s: %s — поток уходит", domain, exc)
                return
            continue
        except Exception as exc:  # noqa: BLE001 — одно окно не должно ронять прогон
            log.exception(
                "%s %s %s: окно не собралось",
                task.court_domain,
                task.cartoteka_id,
                task.window_from,
            )
            task_queue.complete(engine, task, status="failed", error=f"{type(exc).__name__}: {exc}")
            totals.add(failed=True)
            done += 1
            continue

        task_queue.complete(
            engine,
            task,
            status=_TASK_STATUS.get(result.status, "failed"),
            cases_found=result.cases,
            run_id=result.run_id,
            error=result.note,
        )
        totals.add(
            cases=result.cases,
            acts=result.acts,
            failed=result.status in ("short", "failed"),
            throttled=result.status == "throttled",
        )
        done += 1


def run_queue(
    *,
    settings: Settings | None = None,
    only_courts: list[str] | None = None,
    bulk: bool = True,
    limit_per_court: int | None = None,
) -> RunTotals:
    """Обойти очередь. Возвращает итоги прогона."""
    settings = settings or default_settings
    engine = create_engine(settings.database_url, pool_size=12, max_overflow=4)

    released = task_queue.release_stale(engine)
    if released:
        log.info("возвращено в очередь после прошлого прогона: %d окон", released)

    domains = [c.domain for c in courts() if only_courts is None or c.domain in only_courts]
    totals, stop = RunTotals(), threading.Event()

    threads = [
        threading.Thread(
            target=_work_one_court,
            args=(engine, domain, totals, settings, bulk, limit_per_court, stop),
            name=f"обход-{domain}",
            daemon=True,
        )
        for domain in domains
    ]

    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:  # pragma: no cover — сценарий человека
        log.warning("прерывание: доводим текущие окна и выходим")
        stop.set()
        for thread in threads:
            thread.join(timeout=120)
    finally:
        engine.dispose()

    return totals
