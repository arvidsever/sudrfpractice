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
from ..models import CaseCard, CaseRow
from ..raw import RawRecord
from .schema import (
    act,
    appeal,
    case,
    harvest_run,
    hearing,
    participant,
    raw_page,
)


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
        updates = {
            key: statement.excluded[key]
            for key in values
            if key not in ("court_domain", "case_uid", "act_published")
        }
        # Знание о том, что акт опубликован, только прибавляется.
        # Одно и то же дело приходит по двум осям: по публикации — со
        # ссылкой на текст, по поступлению — чаще без неё. Простая замена
        # стирала бы «акт есть» на «не знаем» в зависимости от того, какой
        # проход отработал последним.
        updates["act_published"] = func.coalesce(
            statement.excluded.act_published, case.c.act_published
        )
        statement = statement.on_conflict_do_update(
            index_elements=["court_domain", "case_uid"], set_=updates
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
        without_downgrade = dict(values)
        if values["act_published"] is None:
            without_downgrade.pop("act_published")
        connection.execute(update(case).where(case.c.id == existing).values(**without_downgrade))
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
                index_elements=["case_pk", "text_number"],
                set_={
                    "kind": statement.excluded.kind,
                    "url": statement.excluded.url,
                    "doc_number": statement.excluded.doc_number,
                },
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


def save_card(connection: Connection, case_pk: int, card: CaseCard) -> None:
    """Записать данные карточки: реквизиты, нижестоящий суд, участников, слушания.

    Участники и слушания переписываются целиком: карточка — источник истины,
    и частичное обновление оставляло бы призраков от прошлых разборов.
    """
    values = {
        "category": card.category,
        "appealed_act": card.appealed_act,
        "appeal_result": card.appeal_result,
        "card_fetched_at": datetime.now(UTC),
    }
    if card.lower_court is not None:
        values |= {
            "lower_region": card.lower_court.region,
            "lower_court": card.lower_court.court,
            "lower_case_number": card.lower_court.case_number,
            "lower_decision_date": parse_date(card.lower_court.decision_date),
        }
    # Карточку открыли — значит про акт мы теперь ЗНАЕМ: он либо есть,
    # либо не опубликован по 262-ФЗ. Это тот случай, когда false честен.
    values["act_published"] = card.has_act_text

    connection.execute(update(case).where(case.c.id == case_pk).values(**values))

    connection.execute(participant.delete().where(participant.c.case_pk == case_pk))
    if card.participants:
        connection.execute(
            participant.insert(),
            [
                {
                    "case_pk": case_pk,
                    "role": person.role,
                    "name": person.name,
                    "articles": person.articles,
                    "outcome": person.outcome,
                    "inn": person.inn,
                    "kpp": person.kpp,
                    "ogrn": person.ogrn,
                    "ogrnip": person.ogrnip,
                }
                for person in card.participants
            ],
        )

    connection.execute(appeal.delete().where(appeal.c.case_pk == case_pk))
    if card.appeals:
        connection.execute(
            appeal.insert(),
            [
                {
                    "case_pk": case_pk,
                    "filed_at": parse_date(item.filed_at),
                    "applicant_status": item.applicant_status,
                    "applicant": item.applicant,
                    "passed_to_study_at": parse_date(item.passed_to_study_at),
                    "with_case_request": item.with_case_request,
                    "ruling_date": parse_date(item.ruling_date),
                    "study_result": item.study_result,
                }
                for item in card.appeals
            ],
        )

    connection.execute(hearing.delete().where(hearing.c.case_pk == case_pk))
    if card.hearings:
        connection.execute(
            hearing.insert(),
            [
                {
                    "case_pk": case_pk,
                    "event": item.event,
                    "hearing_date": parse_date(item.date),
                    "hearing_time": item.time,
                    "place": item.place,
                    "result": item.result,
                    "published_at": parse_date(item.published_at),
                }
                for item in card.hearings
            ],
        )


def act_for_text(connection: Connection, case_pk: int, text_number: int) -> int:
    """Строка акта под номером вкладки; заводится, если её ещё нет.

    Дела с оси поступления приходят без ссылки на акт — там `doc_number`
    и `url` останутся пустыми, и это нормально: номер документа нужен
    только для прямой ссылки, а identity держится на номере вкладки.
    """
    statement = insert(act).values(case_pk=case_pk, text_number=text_number)
    statement = statement.on_conflict_do_update(
        index_elements=["case_pk", "text_number"],
        set_={"text_number": statement.excluded.text_number},
    ).returning(act.c.id)
    return connection.execute(statement).scalar_one()
