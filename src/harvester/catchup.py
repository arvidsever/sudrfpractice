"""Инкрементальный добег: забрать то, что опубликовано на днях.

Ось публикации выбрана для этого не случайно. Акт публикуется много позже
рассмотрения — иногда на месяцы, — поэтому окно по дате поступления или
рассмотрения пришлось бы каждый раз перечитывать целиком, чтобы поймать
дозревшие тексты. Окно по дате ПУБЛИКАЦИИ ловит ровно то, что появилось,
и ничего лишнего.

Отсюда и размер работы: за сутки по всем судам публикуется несколько сотен
актов, то есть добег стоит десятки запросов, а не сотни тысяч. Его место —
ежедневное расписание, а не ручной запуск.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import Settings
from .config import settings as default_settings
from .directories import cartoteki, courts
from .harvest import harvest_listing
from .http import CourtOnCooldown
from .urls import DateAxis

log = logging.getLogger("harvester.catchup")

#: С запасом назад: портал иногда публикует задним числом, и окно строго
#: «за вчера» такие акты потеряло бы молча.
DEFAULT_LOOKBACK_DAYS = 3


@dataclass
class CatchupResult:
    windows: int = 0
    cases: int = 0
    acts: int = 0
    skipped: int = 0
    failed: int = 0
    problems: list[str] = field(default_factory=list)


def catchup(
    *,
    settings: Settings | None = None,
    days: int = DEFAULT_LOOKBACK_DAYS,
    today: date | None = None,
    only_courts: list[str] | None = None,
    bulk: bool = True,
) -> CatchupResult:
    """Обойти окно последних `days` дней по оси публикации."""
    settings = settings or default_settings
    end = today or date.today()
    start = end - timedelta(days=days)
    result = CatchupResult()

    log.info("добег за %s — %s", start, end)

    for court in courts():
        if only_courts is not None and court.domain not in only_courts:
            continue
        for cartoteka in cartoteki():
            try:
                run = harvest_listing(
                    court,
                    cartoteka,
                    DateAxis.PUBLICATION,
                    start,
                    end,
                    settings=settings,
                    bulk=bulk,
                )
            except CourtOnCooldown as exc:
                # Суд на паузе: добег вернётся завтра, окно с запасом
                # назад его всё равно поймает.
                result.skipped += 1
                log.info("%s: пропуск — %s", court.domain, exc)
                break
            except Exception as exc:  # noqa: BLE001 — одна картотека не роняет добег
                result.failed += 1
                result.problems.append(f"{court.domain}/{cartoteka.id}: {exc}")
                log.exception("%s/%s: добег не удался", court.domain, cartoteka.id)
                continue

            result.windows += 1
            result.cases += run.cases
            result.acts += run.acts
            if run.status not in ("complete", "empty"):
                result.problems.append(f"{court.domain}/{cartoteka.id}: {run.status}")

    return result
