"""Сбор карточек дел: тексты актов и всё, чего нет в перечне.

Карточка стоит столько же, сколько страница акта, — один запрос, — но
отдаёт сверх текста участников с реквизитами, суд первой инстанции
и движение по событиям. При нескольких актах на дело она отдаёт все
вкладки разом, тогда как `name_op=doc` требует запроса на каждую.
И капчей она не защищена ни на одном суде: на капча-судах капча нужна
только для перечня.

Полнота свода измеряется базой, а не памятью о прогоне: берутся дела
с непроставленным `card_fetched_at`. Прерванный сбор продолжается сам,
повторный запуск ничего не перекачивает.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert

from .client import open_client
from .config import Settings
from .config import settings as default_settings
from .db.schema import act_text, case
from .db.store import act_for_text, save_card, save_raw_page
from .directories import cartoteka as find_cartoteka
from .directories import court as find_court
from .guards import Verdict, classify
from .parse.card import parse_card
from .raw import RawStore
from .urls import card_url

log = logging.getLogger("harvester.cards")

#: Ниже этого числа знаков текст считается подозрительным: кассационные
#: акты — тысячи знаков. Короткий ответ значит, что мы получили не акт.
MIN_PLAUSIBLE_LENGTH = 200


@dataclass(slots=True)
class CardSweepResult:
    court_domain: str
    attempted: int
    cards: int
    texts: int
    participants: int
    without_text: int
    failed: int
    remaining: int
    throttled: bool = False


def pending_cards(
    connection,
    court_domain: str,
    limit: int | None = None,
    cartoteka_id: str | None = None,
):
    """Дела этого суда, чью карточку ещё не открывали."""
    conditions = [
        case.c.court_domain == court_domain,
        case.c.card_fetched_at.is_(None),
        case.c.case_id.is_not(None),
        case.c.case_uid.is_not(None),
    ]
    if cartoteka_id is not None:
        conditions.append(case.c.cartoteka_id == cartoteka_id)

    query = (
        select(case.c.id, case.c.case_id, case.c.case_uid, case.c.cartoteka_id, case.c.case_number)
        .where(*conditions)
        .order_by(case.c.id)
    )
    if limit is not None:
        query = query.limit(limit)
    return connection.execute(query).all()


def collect_cards(
    court_domain: str,
    *,
    settings: Settings | None = None,
    limit: int | None = None,
    bulk: bool = True,
    cartoteka_id: str | None = None,
) -> CardSweepResult:
    settings = settings or default_settings
    raw_store = RawStore(settings.raw_root)
    engine = create_engine(settings.database_url)
    court = find_court(court_domain)

    with engine.connect() as connection:
        targets = pending_cards(connection, court_domain, limit, cartoteka_id)

    cards = texts = participants = without_text = failed = 0
    throttled = False

    with open_client(court, settings=settings, bulk=bulk) as client:
        for row in targets:
            cartoteka = find_cartoteka(row.cartoteka_id)
            url = card_url(
                court, row.case_id, row.case_uid, cartoteka.listing_delo_id, cartoteka.new
            )
            try:
                response = client.get(url)
            except Exception as exc:  # noqa: BLE001 — одна карточка не роняет свод
                failed += 1
                log.warning("%s: %s", row.case_number, exc)
                continue

            html = response.text
            if classify(html).verdict is Verdict.THROTTLED:
                throttled = True
                log.warning("суд ответил «Информация временно недоступна», свод остановлен")
                break

            record = raw_store.save(
                response.content,
                url=url,
                court_domain=court_domain,
                http_status=response.status_code,
                content_kind="card",
            )
            card = parse_card(html)

            with engine.begin() as connection:
                raw_id = save_raw_page(connection, record)
                save_card(connection, row.id, card)
                participants += len(card.participants)

                stored_here = 0
                for text_number, text in sorted(card.act_texts.items()):
                    if len(text) < MIN_PLAUSIBLE_LENGTH:
                        continue
                    act_id = act_for_text(connection, row.id, text_number)
                    statement = insert(act_text).values(
                        act_id=act_id, raw_page_id=raw_id, plain_text=text
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
                    stored_here += 1

            texts += stored_here
            cards += 1
            if stored_here == 0:
                # 262-ФЗ: публикуется не всё. Карточку мы открыли, значит
                # знание есть — просто текста в ней нет.
                without_text += 1

    with engine.connect() as connection:
        remaining = len(pending_cards(connection, court_domain, cartoteka_id=cartoteka_id))
    engine.dispose()

    return CardSweepResult(
        court_domain=court_domain,
        attempted=len(targets),
        cards=cards,
        texts=texts,
        participants=participants,
        without_text=without_text,
        failed=failed,
        remaining=remaining,
        throttled=throttled,
    )
