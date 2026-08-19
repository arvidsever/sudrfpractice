"""Сбор текстов судебных актов по ссылкам из перечня.

Полнота здесь измеряется не счётчиком, а самой базой: у каждой строки `act`
должна появиться строка `act_text`. Акт без текста — это работа, которая
ещё не сделана, и её видно запросом, а не памятью о прогоне.

Дважды один и тот же текст не качается: выборка берёт только те акты,
у которых текста ещё нет. Прерванный сбор продолжается с того же места
без специального учёта.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import create_engine, select, update
from sqlalchemy.dialects.postgresql import insert

from .client import open_client
from .config import Settings
from .config import settings as default_settings
from .db.schema import act, act_text, case
from .db.store import save_raw_page
from .directories import court as find_court
from .guards import Verdict, classify
from .parse.act import parse_act
from .raw import RawStore

log = logging.getLogger("harvester.acts")

#: Ниже этого числа знаков текст считается подозрительным: настоящие
#: кассационные акты — тысячи знаков. Короткий ответ значит, что мы получили
#: не акт, а что-то ещё, и записывать это в базу нельзя.
MIN_PLAUSIBLE_LENGTH = 200


@dataclass(slots=True)
class ActSweepResult:
    court_domain: str
    attempted: int
    stored: int
    empty: int
    failed: int
    remaining: int
    throttled: bool = False


def pending_acts(connection, court_domain: str, limit: int | None = None):
    """Акты этого суда, у которых ещё нет текста."""
    query = (
        select(act.c.id, act.c.doc_number, act.c.text_number, act.c.url, act.c.case_pk)
        .join(case, case.c.id == act.c.case_pk)
        .outerjoin(act_text, act_text.c.act_id == act.c.id)
        .where(case.c.court_domain == court_domain, act_text.c.act_id.is_(None))
        .order_by(act.c.id)
    )
    if limit is not None:
        query = query.limit(limit)
    return connection.execute(query).all()


def collect_act_texts(
    court_domain: str,
    *,
    settings: Settings | None = None,
    limit: int | None = None,
    bulk: bool = True,
) -> ActSweepResult:
    """Скачать и разобрать тексты актов, у которых их ещё нет."""
    settings = settings or default_settings
    raw_store = RawStore(settings.raw_root)
    engine = create_engine(settings.database_url)

    with engine.connect() as connection:
        targets = pending_acts(connection, court_domain, limit)

    stored = empty = failed = 0
    throttled = False

    with open_client(find_court(court_domain), settings=settings, bulk=bulk) as client:
        for row in targets:
            try:
                response = client.get_passing_captcha(row.url)
            except Exception as exc:  # noqa: BLE001 — один акт не должен ронять свод
                failed += 1
                log.warning("акт %s: %s", row.doc_number, exc)
                continue

            html = response.text
            if classify(html).verdict is Verdict.THROTTLED:
                # Суд попросил перестать. Продолжать — значит копить блокировку.
                throttled = True
                log.warning("суд ответил «Информация временно недоступна», сбор остановлен")
                break

            record = raw_store.save(
                response.content,
                url=row.url,
                court_domain=court_domain,
                http_status=response.status_code,
                content_kind="act",
            )

            try:
                parsed = parse_act(html, number=row.doc_number, text_number=row.text_number)
            except ValueError as exc:
                failed += 1
                log.warning("акт %s не разобрался: %s", row.doc_number, exc)
                continue

            if parsed.is_empty_document or len(parsed.text) < MIN_PLAUSIBLE_LENGTH:
                # «ПУСТОЙ ДОКУМЕНТ» — признак конца перебора text_number.
                # Пустое в базу не пишем: пусть остаётся видно как несделанное.
                empty += 1
                log.info("акт %s: пустой документ (%d знаков)", row.doc_number, len(parsed.text))
                continue

            with engine.begin() as connection:
                raw_id = save_raw_page(connection, record)
                statement = insert(act_text).values(
                    act_id=row.id, raw_page_id=raw_id, plain_text=parsed.text
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["act_id"],
                        set_={
                            "plain_text": statement.excluded.plain_text,
                            "raw_page_id": statement.excluded.raw_page_id,
                        },
                    )
                )
                connection.execute(
                    update(case).where(case.c.id == row.case_pk).values(act_published=True)
                )
            stored += 1

    with engine.connect() as connection:
        remaining = len(pending_acts(connection, court_domain))
    engine.dispose()

    return ActSweepResult(
        court_domain=court_domain,
        attempted=len(targets),
        stored=stored,
        empty=empty,
        failed=failed,
        remaining=remaining,
        throttled=throttled,
    )
