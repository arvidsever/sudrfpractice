"""Судебный УИД у уже разобранных карточек (миграция 0013).

Новым карточкам его пишет `save_card`. Полутора миллионам старых — этот
проход: сырьё карточек лежит в S3, разбирается тем же `parse_card`,
новых запросов к судам нет ни одного. Это и есть смысл неизменяемого
сырья: поле, забытое при разборе, достаётся без повторного сбора.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import zstandard
from sqlalchemy import Engine, text

from .archive import PREFIX_RAW
from .http import Response
from .parse.card import parse_card

log = logging.getLogger("harvester.juid")

#: Очередь — карточки, у которых УИД ещё не смотрели. Сырьё находится
#: по адресу карточки; у дела их бывает несколько (перечитывали) — берём
#: последнюю.
PENDING = text(
    """
    SELECT DISTINCT ON (k.id) k.id, r.path, r.url
    FROM "case" k
    JOIN raw_page r ON r.url = k.card_url AND r.content_kind = 'card'
    WHERE k.judicial_uid IS NULL AND k.card_fetched_at IS NOT NULL
    ORDER BY k.id, r.id DESC
    LIMIT :limit
    """
)


def _uid(blob: bytes, url: str) -> str:
    html = Response(
        url=url, status_code=200, content=zstandard.ZstdDecompressor().decompress(blob)
    ).text
    return parse_card(html).case_uid or ""


def backfill(
    engine: Engine,
    raw_root: Path,
    store,
    *,
    batch: int = 5000,
    workers: int = 16,
    limit: int | None = None,
) -> dict[str, int]:
    """Дозаполнить УИД. Прерванный проход продолжается сам: очередь —
    это `judicial_uid IS NULL`."""
    result = {"found": 0, "absent": 0, "unread": 0}

    def load(row) -> tuple[int, str | None]:
        local = raw_root / row.path
        try:
            blob = local.read_bytes() if local.exists() else store.get_bytes(PREFIX_RAW + row.path)
            return row.id, _uid(blob, row.url)
        except Exception as exc:  # noqa: BLE001 — одна страница не роняет проход
            log.warning("%s: %s", row.path, exc)
            return row.id, None

    skipped: set[int] = set()
    with ThreadPoolExecutor(workers) as pool:
        while limit is None or sum(result.values()) < limit:
            with engine.connect() as connection:
                rows = connection.execute(PENDING, {"limit": batch + len(skipped)}).all()
            rows = [row for row in rows if row.id not in skipped]
            if not rows:
                break
            done = list(pool.map(load, rows))
            # Непрочитанное остаётся NULL — возьмём следующим проходом,
            # а в этом больше не трогаем, иначе цикл не кончится.
            skipped |= {pk for pk, uid in done if uid is None}
            found = [{"pk": pk, "uid": uid} for pk, uid in done if uid is not None]
            if found:
                with engine.begin() as connection:
                    connection.execute(
                        text('UPDATE "case" SET judicial_uid = :uid WHERE id = :pk'), found
                    )
            result["found"] += sum(1 for row in found if row["uid"])
            result["absent"] += sum(1 for row in found if not row["uid"])
            result["unread"] = len(skipped)
            log.info("УИД: %s", result)
    return result
