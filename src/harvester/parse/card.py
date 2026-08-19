"""Разбор карточки дела (`name_op=case`).

Карточка отдаёт за один запрос больше, чем страница акта: сверх текста —
участников с реквизитами, нижестоящий суд и движение по событиям. Поэтому
свод текстов идёт через неё, а не через `name_op=doc`, хотя стоят они
одинаково.

Ещё два довода за карточку: при нескольких актах на дело она отдаёт все
вкладки разом, а `name_op=doc` требует запроса на каждую; и капчей она
не защищена ни на одном суде, так что на капча-судах капча нужна только
для перечня.

Разметка: пять таблиц, все с одинаковым `id="tablcont"` (это особенность
платформы, а не опечатка), различаются заголовком в первой строке.
Две устроены как «ключ — значение», три как сетка с шапкой в `td`.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser, Node

from ..models import Appeal, CaseCard, Hearing, LowerCourt, Participant

_DETAILS = "ДЕЛО"
_LOWER = "РАССМОТРЕНИЕ В НИЖЕСТОЯЩЕМ СУДЕ"
_HEARINGS = "СЛУШАНИЯ"
_APPEALS = "ЖАЛОБЫ"

#: Как называется таблица участников — зависит от вида производства.
#: Проверено живьём на 5 КСОЮ 19.08.2026:
#:   гражданские и КАС — «УЧАСТНИКИ»
#:   уголовные        — «ЛИЦА» (с перечнем статей) и «СТОРОНЫ»
#:   КоАП             — «СТОРОНЫ ПО ДЕЛУ» (с перечнем статей)
#: Искать только «УЧАСТНИКОВ» значило бы молча терять всех участников
#: по уголовным и КоАП — счётчики при этом сошлись бы.
_PARTICIPANT_TABLES = ("УЧАСТНИКИ", "СТОРОНЫ ПО ДЕЛУ", "СТОРОНЫ", "ЛИЦА")

#: У КоАП «СЛУШАНИЙ» и «ЖАЛОБ» может не быть вовсе — это не сбой разбора.

#: Подписи в таблице «ДЕЛО» → поля карточки. Подписи берутся целиком,
#: без сокращений: портал их не меняет, а частичное совпадение однажды
#: подцепит соседнюю строку.
_DETAIL_FIELDS = {
    "Уникальный идентификатор дела": "case_uid",
    "Дата поступления": "receipt_date",
    "Категория дела": "category",
    "Вид обжалуемого судебного акта": "appealed_act",
    "Судья": "judge",
    "Дата рассмотрения": "decision_date",
    "Результат рассмотрения": "result",
    "Результат в отношении решения апелляционной инстанции": "appeal_result",
}

_LOWER_FIELDS = {
    "Регион суда первой инстанции": "region",
    "Суд (судебный участок) первой инстанции": "court",
    "Номер дела в первой инстанции": "case_number",
    "Дата решения первой инстанции": "decision_date",
}


def _text(node: Node) -> str:
    return " ".join(node.text(separator=" ").split())


def _tables(tree: HTMLParser) -> dict[str, Node]:
    """Таблицы карточки по заголовку первой строки."""
    found: dict[str, Node] = {}
    for table in tree.css("table"):
        headers = table.css("th")
        if headers:
            found.setdefault(_text(headers[0]), table)
    return found


def _key_values(table: Node) -> dict[str, str]:
    values: dict[str, str] = {}
    for row in table.css("tr"):
        cells = row.css("td")
        if len(cells) == 2:
            key, value = _text(cells[0]), _text(cells[1])
            if key:
                values[key] = value
    return values


def _pick(row: dict[str, str], label: str) -> str | None:
    """Значение по НАЧАЛУ заголовка колонки.

    Точное сравнение здесь не годится: у колонки «Дата размещения» портал
    приклеивает к заголовку текст всплывающей подсказки, и заголовок
    получается длиной в предложение.
    """
    for header, value in row.items():
        if header.startswith(label):
            return value or None
    return None


def _grid(table: Node) -> list[dict[str, str]]:
    """Сетка с шапкой в первой строке из `td`, а не `th`."""
    rows = [row.css("td") for row in table.css("tr")]
    rows = [cells for cells in rows if cells]
    if len(rows) < 2:
        return []
    header = [_text(cell) for cell in rows[0]]
    return [dict(zip(header, (_text(cell) for cell in cells), strict=False)) for cells in rows[1:]]


def act_texts(tree: HTMLParser) -> dict[int, str]:
    """Тексты актов из вкладок `cont_doc{N}`.

    Пусто — законное состояние: по 262-ФЗ публикуется не всё.
    """
    texts: dict[int, str] = {}
    for number in range(1, 21):
        block = tree.css_first(f"#cont_doc{number}")
        if block is None:
            break
        for junk in block.css("script, style"):
            junk.decompose()
        texts[number] = _text(block)
    return texts


def _participant(row: dict[str, str]) -> Participant | None:
    """Строка участника из любой из четырёх таблиц.

    Имя лица подписано по-разному: «Фамилия / наименование» у большинства,
    «Лицо, участвующее в деле (ФИО)» в «СТОРОНАХ» уголовной карточки.
    """
    name = _pick(row, "Фамилия") or _pick(row, "Лицо, участвующее в деле") or ""
    role = _pick(row, "Вид лица") or ""
    if not (name or role):
        return None
    return Participant(
        # У «ЛИЦ» уголовной карточки колонки вида лица нет вовсе —
        # там все строки об обвиняемых, и роль подставляется явно.
        role=role or "ЛИЦО",
        name=name,
        articles=_pick(row, "Перечень статей"),
        outcome=_pick(row, "Результат в отношении лица"),
        inn=_pick(row, "ИНН"),
        kpp=_pick(row, "КПП"),
        ogrn=_pick(row, "ОГРН "),
        ogrnip=_pick(row, "ОГРНИП"),
    )


def parse_card(html: str) -> CaseCard:
    tree = HTMLParser(html)
    tables = _tables(tree)

    card = CaseCard()

    if (table := tables.get(_DETAILS)) is not None:
        values = _key_values(table)
        for label, field in _DETAIL_FIELDS.items():
            if value := values.get(label):
                setattr(card, field, value)

    if (table := tables.get(_LOWER)) is not None:
        values = _key_values(table)
        lower = {
            field: values[label] for label, field in _LOWER_FIELDS.items() if values.get(label)
        }
        if lower:
            card.lower_court = LowerCourt(**lower)

    for name in _PARTICIPANT_TABLES:
        table = tables.get(name)
        if table is None:
            continue
        for row in _grid(table):
            person = _participant(row)
            if person is not None:
                card.participants.append(person)

    if (table := tables.get(_HEARINGS)) is not None:
        for row in _grid(table):
            event = _pick(row, "Наименование события") or ""
            if not event:
                continue
            card.hearings.append(
                Hearing(
                    event=event,
                    date=_pick(row, "Дата"),
                    time=_pick(row, "Время"),
                    place=_pick(row, "Место проведения"),
                    result=_pick(row, "Результат события"),
                    published_at=_pick(row, "Дата размещения"),
                )
            )

    if (table := tables.get(_APPEALS)) is not None:
        for row in _grid(table):
            appeal = Appeal(
                filed_at=_pick(row, "Дата поступления"),
                applicant_status=_pick(row, "Процессуальный статус"),
                applicant=_pick(row, "Лицо, подавшее жалобу"),
                passed_to_study_at=_pick(row, "Дата передачи жалобы"),
                with_case_request=_pick(row, "С истребованием дела"),
                ruling_date=_pick(row, "Дата вынесения определения"),
                study_result=_pick(row, "Результат изучения жалобы"),
            )
            if any(appeal.model_dump().values()):
                card.appeals.append(appeal)

    card.act_texts = act_texts(tree)
    return card
