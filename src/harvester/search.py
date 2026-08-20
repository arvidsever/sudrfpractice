"""Поиск по индексу: фасеты и выдача.

Смысл корпуса не в том, что он собран, а в том, что по нему можно искать
так, как юрист думает о практике: этот суд, эта картотека, этот судья,
такой результат, такой период, дело пришло из такого-то суда первой
инстанции. Всё это лежит прямо в строке выдачи и не требует ни карточки,
ни текста акта — значит поиск работает уже сейчас, на середине сбора.

Два ограничения, оба намеренные:

* **фильтры складываются через И.** «Или» внутри одного фасета делается
  списком значений (`judge=[...]`), а между фасетами не делается вовсе:
  запрос «уголовные ИЛИ этого судьи» не осмыслен;
* **пустой результат — это ответ, а не ошибка.** Корпус собран не весь,
  и «ничего не нашлось» здесь значит «ничего не нашлось В СОБРАННОМ».
  Отсюда `Found.collected_share`: без неё выдача выглядит полнее, чем есть.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import Engine, Select, func, select

from .db.schema import case

#: Сколько дел отдавать за раз, если не сказано иначе.
PAGE_SIZE = 25

#: Сколько значений показывать в фасете. Судей за три миллиона дел будут
#: тысячи, и вывалить их списком — не помощь.
FACET_LIMIT = 15


@dataclass(frozen=True, slots=True)
class Query:
    """Что ищем. Пустое поле — «неважно», а не «пусто»."""

    courts: tuple[str, ...] = ()
    cartoteki: tuple[str, ...] = ()
    judges: tuple[str, ...] = ()
    results: tuple[str, ...] = ()
    lower_courts: tuple[str, ...] = ()
    number: str | None = None
    decided_from: date | None = None
    decided_to: date | None = None
    #: Только дела с опубликованным актом. `None` — неважно.
    with_act: bool | None = None
    limit: int = PAGE_SIZE
    offset: int = 0


@dataclass(frozen=True, slots=True)
class Found:
    total: int
    rows: list[dict]
    #: Доля собранного корпуса на момент запроса. Не украшение: без неё
    #: «найдено 12» читается как «в природе двенадцать».
    collected_share: float
    facets: dict[str, list[tuple[str, int]]] = field(default_factory=dict)


def _narrow(statement: Select, query: Query) -> Select:
    """Наложить фильтры. Каждый — сужение, все складываются через И."""
    if query.courts:
        statement = statement.where(case.c.court_domain.in_(query.courts))
    if query.cartoteki:
        statement = statement.where(case.c.cartoteka_id.in_(query.cartoteki))
    if query.judges:
        statement = statement.where(case.c.judge.in_(query.judges))
    if query.results:
        statement = statement.where(case.c.result.in_(query.results))
    if query.lower_courts:
        statement = statement.where(case.c.lower_court.in_(query.lower_courts))
    if query.number:
        # Номер дела пишут по-разному, и точное совпадение почти никогда
        # не то, что имеют в виду. Триграммный индекс это и держит.
        statement = statement.where(case.c.case_number.ilike(f"%{query.number}%"))
    if query.decided_from is not None:
        statement = statement.where(case.c.decision_date >= query.decided_from)
    if query.decided_to is not None:
        statement = statement.where(case.c.decision_date <= query.decided_to)
    if query.with_act is True:
        statement = statement.where(case.c.act_published.is_(True))
    if query.with_act is False:
        # Именно «известно, что нет», а не «не проверяли»: `act_published`
        # трёхзначен, и `null` тут не то же самое, что `false`.
        statement = statement.where(case.c.act_published.is_(False))
    return statement


#: Колонки выдачи. Ровно то, по чему дело узнают, — без текста и участников.
COLUMNS = (
    case.c.court_domain,
    case.c.cartoteka_id,
    case.c.case_number,
    case.c.decision_date,
    case.c.judge,
    case.c.result,
    case.c.act_published,
    case.c.card_url,
)

#: Фасеты: как называется и по какой колонке считается.
FACET_COLUMNS = {
    "суд": case.c.court_domain,
    "картотека": case.c.cartoteka_id,
    "судья": case.c.judge,
    "результат": case.c.result,
}


def run(engine: Engine, query: Query, *, with_facets: bool = False) -> Found:
    """Выполнить запрос. `with_facets` считает счётчики по каждому фасету."""
    with engine.connect() as connection:
        total = connection.execute(
            _narrow(select(func.count()).select_from(case), query)
        ).scalar_one()
        collected = connection.execute(select(func.count()).select_from(case)).scalar_one()

        rows = [
            dict(row._mapping)
            for row in connection.execute(
                _narrow(select(*COLUMNS), query)
                # Свежие дела вперёд: практику ищут от нового к старому.
                # `nulls last` — потому что нерассмотренные дела без даты
                # решения иначе всплыли бы первыми.
                .order_by(case.c.decision_date.desc().nulls_last(), case.c.id.desc())
                .limit(query.limit)
                .offset(query.offset)
            )
        ]

        facets: dict[str, list[tuple[str, int]]] = {}
        if with_facets:
            for name, column in FACET_COLUMNS.items():
                facets[name] = [
                    (str(value), count)
                    for value, count in connection.execute(
                        _narrow(select(column, func.count()), query)
                        .where(column.is_not(None))
                        .group_by(column)
                        .order_by(func.count().desc())
                        .limit(FACET_LIMIT)
                    ).all()
                ]

    return Found(
        total=total,
        rows=rows,
        collected_share=total / collected if collected else 0.0,
        facets=facets,
    )
