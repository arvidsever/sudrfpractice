"""Наполнение справочных таблиц из `data/directories/*.json`.

Идемпотентно: справочник ведётся в Swift-репозитории и переезжает сюда
целиком при каждом изменении, поэтому это upsert, а не вставка.
"""

from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert

from ..config import settings
from ..directories import cartoteki, courts
from .schema import cartoteka as cartoteka_table
from .schema import court as court_table


def load() -> tuple[int, int]:
    """Записать суды и картотеки. Возвращает (сколько судов, сколько картотек)."""
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        court_rows = [
            {
                "domain": item.domain,
                "number": item.number,
                "title": item.title,
                "level": "cassation",
                "regions": json.dumps(list(item.regions), ensure_ascii=False),
                "has_captcha": item.has_captcha,
            }
            for item in courts()
        ]
        statement = insert(court_table).values(court_rows)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["domain"],
                set_={
                    key: statement.excluded[key]
                    for key in ("number", "title", "level", "regions", "has_captcha")
                },
            )
        )

        cartoteka_rows = [
            {
                "id": item.id,
                "title": item.title,
                "delo_id": item.delo_id,
                "new": item.new,
                "delo_table": item.delo_table,
                "doc_prefix": item.doc_prefix,
            }
            for item in cartoteki()
        ]
        statement = insert(cartoteka_table).values(cartoteka_rows)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    key: statement.excluded[key]
                    for key in ("title", "delo_id", "new", "delo_table", "doc_prefix")
                },
            )
        )
    engine.dispose()
    return len(court_rows), len(cartoteka_rows)
