"""Запись собранного в базу.

Всё через upsert: обход одного и того же окна должен быть безопасен для
повторения. Харвестер будет перечитывать окна — из-за доработки парсера,
из-за обрыва связи, из-за нового акта у старого дела, — и второй проход
обязан обновлять, а не задваивать.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from ..dates import parse as parse_date
from ..models import CaseRow
from ..raw import RawRecord
from .schema import act, act_text, case, harvest_run, raw_page


def save_raw_page(connection: Connection, record: RawRecord) -> int:
    """Записать провенанс страницы. Повторная страница не задваивается: ключ — sha256."""
    statement = insert(raw_page).values(
        sha256=record.sha256,
        url=record.url,
        court_domain=record.court_domain,
        fetched_at=datetime.fromisoformat(record.fetched_at),
        http_status=record.http_status,
        byte_size=record.byte_size,
        content_kind=record.content_kind,
        path=record.path,
    )
    statement = statement.on_conflict_do_update(
        index_elements=["sha256"],
        set_={"fetched_at": statement.excluded.fetched_at, "url": statement.excluded.url},
    ).returning(raw_page.c.id)
    return connection.execute(statement).scalar_one()


def save_case(connection: Connection, row: CaseRow, *, court_domain: str, cartoteka_id: str) -> int:
    """Записать дело и вернуть его первичный ключ.

    Ключ — пара (суд, УИД). Номер дела для этого не годится: он повторяется
    между судами и меняет вид («8Г-…» и «[88-…]» в одной ячейке).
    """
    values = {
        "court_domain": court_domain,
        "cartoteka_id": cartoteka_id,
        "case_id": row.case_id,
        "case_uid": row.case_uid,
        "case_number": row.case_number,
        "receipt_date": parse_date(row.receipt_date),
        "essence": row.essence,
        "judge": row.judge,
        "decision_date": parse_date(row.decision_date),
        "result": row.result,
        "legal_force_date": parse_date(row.legal_force_date),
        "card_url": row.card_url,
        # None, а не False: пустая последняя колонка перечня больше не означает
        # «акт не опубликован» — она не означает ничего. См. миграцию 0002.
        "act_published": True if row.has_published_act else None,
        "last_seen": datetime.now(UTC),
    }

    if row.case_uid:
        statement = insert(case).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["court_domain", "case_uid"],
            set_={
                key: statement.excluded[key]
                for key in values
                if key not in ("court_domain", "case_uid")
            },
        ).returning(case.c.id)
        return connection.execute(statement).scalar_one()

    # Дело без УИД: уникальность в базе не выразить, поэтому ищем руками.
    # Такие строки встречаются редко, и терять их нельзя.
    existing = connection.execute(
        select(case.c.id).where(
            case.c.court_domain == court_domain,
            case.c.case_number == row.case_number,
            case.c.case_uid.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        connection.execute(update(case).where(case.c.id == existing).values(**values))
        return existing
    return connection.execute(insert(case).values(**values).returning(case.c.id)).scalar_one()


def save_acts(connection: Connection, case_pk: int, row: CaseRow) -> int:
    """Записать ссылки на тексты актов. Возвращает число записанных ссылок."""
    if not row.act_links:
        return 0
    for link in row.act_links:
        statement = insert(act).values(
            case_pk=case_pk,
            doc_number=link.number,
            text_number=link.text_number,
            kind=link.kind,
            url=link.url,
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["case_pk", "doc_number", "text_number"],
                set_={"kind": statement.excluded.kind, "url": statement.excluded.url},
            )
        )
    return len(row.act_links)


def open_run(
    connection: Connection,
    *,
    court_domain: str,
    cartoteka_id: str,
    axis: str,
    window_from,
    window_to,
) -> int:
    """Открыть запись обхода. Она заводится ДО первого запроса намеренно:
    прерванный обход должен остаться в журнале как незакрытый, а не исчезнуть."""
    return connection.execute(
        insert(harvest_run)
        .values(
            court_domain=court_domain,
            cartoteka_id=cartoteka_id,
            axis=axis,
            window_from=window_from,
            window_to=window_to,
            status="running",
        )
        .returning(harvest_run.c.id)
    ).scalar_one()


def close_run(
    connection: Connection,
    run_id: int,
    *,
    expected_count: int | None,
    fetched_rows: int,
    pages_done: int,
    status: str,
    note: str | None = None,
) -> None:
    connection.execute(
        update(harvest_run)
        .where(harvest_run.c.id == run_id)
        .values(
            expected_count=expected_count,
            fetched_rows=fetched_rows,
            pages_done=pages_done,
            status=status,
            note=note,
            finished_at=func.now(),
        )
    )


def act_text_count(connection: Connection) -> int:
    return connection.execute(select(func.count()).select_from(act_text)).scalar_one()
