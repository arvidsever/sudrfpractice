"""Судебный УИД: ключ связи между инстанциями, достаётся из сырья карточек."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, insert, select

from harvester.db.schema import case
from harvester.db.store import save_raw_page
from harvester.juid import backfill
from harvester.raw import RawStore

CARD = (
    "https://5kas.sudrf.ru/modules.php?name=sud_delo&name_op=case&case_id=1&case_uid=g-1&delo_id=42"
)
# Запрос уходит с `new=0`, которого в ссылке из выдачи нет.
URL = CARD.replace("&delo_id", "&new=0&delo_id")


class _NoS3:
    def get_bytes(self, key: str) -> bytes:
        raise FileNotFoundError(key)


def test_uid_is_backfilled_from_raw_cards(db_settings, card_criminal) -> None:
    engine = create_engine(db_settings.database_url)
    store = RawStore(db_settings.raw_root)
    record = store.save(
        card_criminal.encode("cp1251", errors="replace"),
        url=URL,
        court_domain="5kas.sudrf.ru",
        http_status=200,
        content_kind="card",
    )
    with engine.begin() as connection:
        save_raw_page(connection, record)
        for number, url in (("77-1/2026", CARD), ("77-2/2026", CARD + "x")):
            connection.execute(
                insert(case).values(
                    court_domain="5kas.sudrf.ru",
                    cartoteka_id="u3",
                    case_uid=number,
                    case_number=number,
                    card_url=url,
                    card_fetched_at=datetime.now(UTC),
                )
            )

    result = backfill(engine, db_settings.raw_root, _NoS3())
    assert result == {"found": 1, "absent": 0, "unread": 0}
    with engine.connect() as connection:
        uids = dict(connection.execute(select(case.c.case_number, case.c.judicial_uid)).all())
    # У второго дела сырья нет вовсе — оно остаётся «не смотрели», а не «УИД нет».
    assert uids == {"77-1/2026": "26RS0001-01-2024-012553-83", "77-2/2026": None}
    engine.dispose()
