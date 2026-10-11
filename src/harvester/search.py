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

from dataclasses import dataclass, field, replace
from datetime import date

from sqlalchemy import Engine, Select, Text, and_, cast, func, literal_column, select
from sqlalchemy.dialects.postgresql import ARRAY, array

from .db.schema import (
    act,
    act_text,
    case,
    ksrf_decision,
    ksrf_decision_text,
    norm_ref,
    vsrf_act,
    vsrf_act_text,
    vsrf_claim,
)

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
    #: Слова в тексте акта. Синтаксис `websearch_to_tsquery`: кавычки —
    #: фраза целиком, `-слово` — исключение, `or` — альтернатива.
    text: str | None = None
    #: Нормы, на которые акт обязан ссылаться: ключи `ГК:333` из
    #: `norms.parse_query`. Все сразу — складываются через И.
    norms: tuple[str, ...] = ()
    #: `relevance` — по плотности совпадения среди самых свежих совпавших
    #: актов (только при поиске по словам); `date` — от свежего к старому.
    sort: str = "relevance"
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
    #: Доля актов, чей текст уже разобран, — и она же потолок полнотекста.
    #: Ссылок на акты 1,6 млн, а текстов пока 375 тысяч: поиск по словам
    #: видит четверть того, что видит поиск по реквизитам. Считается
    #: только когда о текстах и спрашивают.
    texts_share: float = 0.0
    facets: dict[str, list[tuple[str, int]]] = field(default_factory=dict)


#: Словарь, которым нарезан индекс. Он же стоит в `tsvector`-колонке,
#: и разойтись они не имеют права: тогда запрос перестанет попадать
#: в индекс и молча уедет на перебор восьми гигабайт текста.
REGCONFIG = literal_column("'russian'")

#: Колонку `tsv` держит миграция, а не схема: это generated-колонка,
#: и описывать её в двух местах — верный способ однажды разойтись.
TSV = literal_column("act_text.tsv")


def _tsquery(text: str):
    return func.websearch_to_tsquery(REGCONFIG, text)


def _act_matches(query: Query) -> list:
    """Условия на текст акта: слова и нормы. Оба — про один и тот же акт:
    «неустойка» в одном акте дела и «ГК:333» в другом — не совпадение."""
    conditions = []
    if query.text:
        conditions.append(TSV.op("@@")(_tsquery(query.text)))
    if query.norms:
        conditions.append(
            select(literal_column("1"))
            .where(
                norm_ref.c.source == "act",
                norm_ref.c.doc_id == act_text.c.act_id,
                norm_ref.c.norms.contains(cast(array(query.norms), ARRAY(Text))),
            )
            .exists()
        )
    return conditions


def _matching_acts(query: Query):
    """Есть ли у дела акт, текст которого отвечает запросу.

    Через `EXISTS`, а не соединением: у дела бывает несколько актов,
    и соединение размножило бы дело по числу совпавших — «найдено 8 675»
    считало бы акты, выдавая их за дела.
    """
    return (
        select(literal_column("1"))
        .select_from(act.join(act_text, act_text.c.act_id == act.c.id))
        .where(act.c.case_pk == case.c.id, *_act_matches(query))
        .exists()
    )


def _snippet(query: Query):
    """Кусок текста вокруг совпадения — иначе непонятно, за что нашлось.

    Считается только для показанных строк: это скалярный подзапрос
    в списке выборки, а не в условии.
    """
    return (
        select(
            func.ts_headline(
                REGCONFIG,
                act_text.c.plain_text,
                _tsquery(query.text),
                "MaxFragments=1, MaxWords=30, MinWords=15, StartSel=«, StopSel=»",
            )
        )
        .select_from(act.join(act_text, act_text.c.act_id == act.c.id))
        .where(act.c.case_pk == case.c.id, *_act_matches(query))
        .limit(1)
        .scalar_subquery()
    )


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
    if query.text or query.norms:
        statement = statement.where(_matching_acts(query))
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


#: Дошло ли дело до Верховного Суда: есть ли там производство с тем же
#: судебным УИД. Считается только для показанных строк.
IN_SUPREME = (
    select(literal_column("1"))
    .where(func.upper(vsrf_claim.c.case_uid) == case.c.judicial_uid, case.c.judicial_uid != "")
    .exists()
    .label("in_supreme")
)

#: Сколько самых свежих совпавших актов ранжируется. Ранг считается по
#: `tsvector` каждого кандидата, а это чтение десятков килобайт на акт:
#: 2 000 — 1,4 с на живом корпусе, все 37 тысяч по «несоразмерности
#: неустойки» — уже десятки секунд.
# ponytail: окно по свежести, а не весь корпус — старый опорный акт вне
# окна не всплывёт; сдвигается датами (`decided_to`). Нужен весь корпус —
# это уже отдельный индекс ранжирования (RUM) или внешний поиск.
RANK_WINDOW = 2000


def _by_relevance(statement: Select, query: Query) -> Select:
    """Дела по убыванию плотности совпадения (`ts_rank_cd`, с поправкой
    на длину текста) среди `RANK_WINDOW` самых свежих совпавших актов.

    Кандидаты берутся от полнотекстового индекса, а не обходом дел по дате:
    второй путь на том же запросе — 15 с против 0,4 с.
    """
    recent = (
        _narrow(
            select(act.c.case_pk, act_text.c.act_id).select_from(
                act_text.join(act, act.c.id == act_text.c.act_id).join(
                    case, case.c.id == act.c.case_pk
                )
            ),
            # Условия на текст ставим сами, прямо на эту строку `act_text`,
            # — иначе `_narrow` добавил бы второй обход через EXISTS.
            replace(query, text=None, norms=()),
        )
        .where(and_(*_act_matches(query)))
        .order_by(case.c.decision_date.desc().nulls_last())
        .limit(RANK_WINDOW)
        .subquery("recent")
    )
    scored = act_text.alias("scored")
    ranked = (
        select(
            recent.c.case_pk,
            func.max(func.ts_rank_cd(literal_column("scored.tsv"), _tsquery(query.text), 1)).label(
                "rank"
            ),
        )
        .select_from(recent.join(scored, scored.c.act_id == recent.c.act_id))
        .group_by(recent.c.case_pk)
        .subquery("ranked")
    )
    return (
        statement.join(ranked, ranked.c.case_pk == case.c.id)
        .order_by(ranked.c.rank.desc(), case.c.decision_date.desc().nulls_last())
        .limit(query.limit)
        .offset(query.offset)
    )


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
    IN_SUPREME,
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
        # Обе доли считаются только тогда, когда о них спросят. Полный
        # счёт `case` — 2,9 с на 2,75 млн строк, и платить их за число,
        # которое не покажут, незачем: при поиске по словам потолок задают
        # тексты, а не индекс.
        collected_share = texts_share = 0.0
        if query.text or query.norms:
            acts = connection.execute(select(func.count()).select_from(act)).scalar_one()
            texts = connection.execute(select(func.count()).select_from(act_text)).scalar_one()
            texts_share = texts / acts if acts else 0.0
        else:
            collected = connection.execute(select(func.count()).select_from(case)).scalar_one()
            collected_share = total / collected if collected else 0.0

        columns = (*COLUMNS, _snippet(query).label("snippet")) if query.text else COLUMNS
        if query.text and query.sort == "relevance":
            statement = _by_relevance(select(*columns), query)
        else:
            statement = (
                _narrow(select(*columns), query)
                # Свежие дела вперёд. `nulls last` — потому что нерассмотренные
                # дела без даты решения иначе всплыли бы первыми.
                .order_by(case.c.decision_date.desc().nulls_last(), case.c.id.desc())
                .limit(query.limit)
                .offset(query.offset)
            )
        rows = [dict(row._mapping) for row in connection.execute(statement)]

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
        collected_share=collected_share,
        texts_share=texts_share,
        facets=facets,
    )


# --- Высшие суды: ВС и КС ---------------------------------------------------
#
# Свои таблицы, а не `case` (docs/vsrf.md, docs/ksrf.md): у акта ВС и решения
# КС нет ни картотеки, ни результата из справочника, ни карточки на sudrf.
# Поэтому и выдача у них своя, рядом с основной, а не вперемешку: фильтры
# по картотеке, судье и результату к ним не относятся.


@dataclass(frozen=True, slots=True)
class _Higher:
    title: str
    source: str  # метка в `norm_ref`
    head: object  # таблица реквизитов
    body: object  # таблица текстов; обе связаны по `pdf_id`
    decided: object  # колонка даты
    kind: object
    about: object
    url: str  # шаблон ссылки на PDF


HIGHER = {
    "www.vsrf.ru": _Higher(
        "Верховный Суд РФ",
        "vsrf",
        vsrf_act,
        vsrf_act_text,
        vsrf_act.c.act_date,
        vsrf_act.c.act_kind,
        vsrf_act.c.subject,
        "https://www.vsrf.ru/lk/practice/stor_pdf/{}",
    ),
    "www.ksrf.ru": _Higher(
        "Конституционный Суд РФ",
        "ksrf",
        ksrf_decision,
        ksrf_decision_text,
        ksrf_decision.c.decision_date,
        ksrf_decision.c.kind,
        ksrf_decision.c.title,
        "https://www.ksrf.ru/doc/KSRFDecision{}.pdf",
    ),
}


def wants_higher(query: Query) -> tuple[str, ...]:
    """Какие высшие суды отвечают на запрос.

    Только поиск по словам или номеру, и только если не названы фасеты,
    которых у ВС и КС нет: «картотека g3» про них ничего не говорит,
    и подмешивать их к такому запросу — значит отвечать не на него.
    """
    if not (query.text or query.number or query.norms):
        return ()
    if query.cartoteki or query.judges or query.results or query.lower_courts:
        return ()
    if query.with_act is not None:
        return ()
    return tuple(d for d in HIGHER if not query.courts or d in query.courts)


def _higher_columns(spec: _Higher) -> list:
    return [
        spec.head.c.pdf_id,
        spec.head.c.number,
        spec.decided.label("decision_date"),
        spec.kind.label("kind"),
        spec.about.label("about"),
    ]


def _higher_row(domain: str, row) -> dict:
    return {
        **row._mapping,
        "court_domain": domain,
        "url": HIGHER[domain].url.format(row.pdf_id),
    }


def run_higher(engine: Engine, query: Query, domain: str) -> Found:
    """Тот же запрос по текстам ВС или КС. `texts_share` — доля актов
    этого суда, чей PDF уже разобран."""
    spec = HIGHER[domain]
    head, body = spec.head, spec.body
    tsv = literal_column(f"{body.name}.tsv")
    joined = head.join(body, body.c.pdf_id == head.c.pdf_id)

    def narrow(statement: Select) -> Select:
        if query.text:
            statement = statement.where(tsv.op("@@")(_tsquery(query.text)))
        if query.norms:
            statement = statement.where(
                select(literal_column("1"))
                .where(
                    norm_ref.c.source == spec.source,
                    norm_ref.c.doc_id == head.c.pdf_id,
                    norm_ref.c.norms.contains(cast(array(query.norms), ARRAY(Text))),
                )
                .exists()
            )
        if query.number:
            statement = statement.where(head.c.number.ilike(f"%{query.number}%"))
        if query.decided_from is not None:
            statement = statement.where(spec.decided >= query.decided_from)
        if query.decided_to is not None:
            statement = statement.where(spec.decided <= query.decided_to)
        return statement

    columns = _higher_columns(spec)
    if query.text:
        columns.append(
            func.ts_headline(
                REGCONFIG,
                body.c.plain_text,
                _tsquery(query.text),
                "MaxFragments=1, MaxWords=30, MinWords=15, StartSel=«, StopSel=»",
            ).label("snippet")
        )
    with engine.connect() as connection:
        total = connection.execute(narrow(select(func.count()).select_from(joined))).scalar_one()
        acts = connection.execute(select(func.count()).select_from(head)).scalar_one()
        texts = connection.execute(select(func.count()).select_from(body)).scalar_one()
        rows = [
            _higher_row(domain, row)
            for row in connection.execute(
                narrow(select(*columns).select_from(joined))
                # Актов у высших судов по запросу сотни, а не десятки тысяч:
                # ранжируются все совпавшие, окно не нужно.
                .order_by(
                    *(
                        [func.ts_rank_cd(tsv, _tsquery(query.text), 1).desc()]
                        if query.text and query.sort == "relevance"
                        else []
                    ),
                    spec.decided.desc().nulls_last(),
                    head.c.pdf_id.desc(),
                )
                .limit(query.limit)
                .offset(query.offset)
            )
        ]
    return Found(
        total=total, rows=rows, collected_share=0.0, texts_share=texts / acts if acts else 0.0
    )


def higher_texts(engine: Engine, number: str, limit: int) -> list[dict]:
    """Тексты актов ВС и КС по номеру — для команды `act`. Не больше
    `limit` на всех."""
    found: list[dict] = []
    with engine.connect() as connection:
        for domain, spec in HIGHER.items():
            head, body = spec.head, spec.body
            found += [
                _higher_row(domain, row)
                for row in connection.execute(
                    select(*_higher_columns(spec), body.c.plain_text)
                    .select_from(head.join(body, body.c.pdf_id == head.c.pdf_id))
                    .where(head.c.number.ilike(f"%{number}%"))
                    .order_by(spec.decided.desc().nulls_last())
                    .limit(max(limit - len(found), 0))
                )
            ]
    return found


# --- Одно дело в разных инстанциях ------------------------------------------
#
# Судебный УИД (`case.judicial_uid`, `vsrf_claim.case_uid`) у дела один
# на все инстанции. Связь — запросом по нему, а не отдельной таблицей:
# хранить нечего, оба конца уже проиндексированы.


def uid_of_supreme_act(engine: Engine, pdf_id: int) -> str | None:
    """Судебный УИД дела, по которому вынесен акт ВС."""
    with engine.connect() as connection:
        return connection.execute(
            select(func.upper(vsrf_claim.c.case_uid))
            .select_from(vsrf_act.join(vsrf_claim, vsrf_claim.c.claim_id == vsrf_act.c.claim_id))
            .where(vsrf_act.c.pdf_id == pdf_id)
        ).scalar()


def instances(engine: Engine, uid: str | None) -> list[str]:
    """Все производства по делу с этим судебным УИД — строками для печати:
    сначала наш корпус (апелляция, кассация), затем Верховный Суд с актами."""
    if not uid:
        return []
    lines = []
    with engine.connect() as connection:
        for row in connection.execute(
            select(
                case.c.court_domain,
                case.c.case_number,
                case.c.decision_date,
                case.c.result,
                case.c.card_url,
            )
            .where(case.c.judicial_uid == uid)
            .order_by(case.c.decision_date.nulls_last())
        ):
            lines.append(
                f"  {row.court_domain}  {row.case_number}  {row.decision_date or '—'}"
                f"  {(row.result or '—')[:70]}"
            )
            if row.card_url:
                lines.append(f"       {row.card_url}")
        for claim in connection.execute(
            select(vsrf_claim.c.claim_id, vsrf_claim.c.number, vsrf_claim.c.received_date)
            .where(func.upper(vsrf_claim.c.case_uid) == uid)
            .order_by(vsrf_claim.c.received_date.nulls_last())
        ):
            lines.append(
                f"  www.vsrf.ru  {claim.number or '—'}  поступило {claim.received_date or '—'}"
            )
            for found in connection.execute(
                select(vsrf_act.c.pdf_id, vsrf_act.c.act_kind, vsrf_act.c.act_date)
                .where(vsrf_act.c.claim_id == claim.claim_id)
                .order_by(vsrf_act.c.act_date.nulls_last())
            ):
                lines.append(
                    f"       {found.act_kind or 'акт'} от {found.act_date or '—'}:"
                    f" {HIGHER['www.vsrf.ru'].url.format(found.pdf_id)}"
                )
    return lines
