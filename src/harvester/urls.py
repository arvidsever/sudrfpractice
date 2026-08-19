"""Сборка URL для модуля `sud_delo` — §1 и §4 грамматики перечня.

Собирать строку запроса руками нельзя: половина граблей платформы сидит
именно здесь. Что зафиксировано в этом модуле раз и навсегда:

* **`vnkod` не отправляется никогда.** Полный набор пустых полей из
  `sudrfscraper` ломает выдачу именно из-за `&vnkod=`: сервер отвечает 200
  и формой поиска. У КСОЮ винтажного VNKOD-интерфейса нет вовсе.
* **`Submit` обязателен** и уходит уже в cp1251.
* **`new` шлём всегда** — у гражданской и уголовной кассации `new=0`
  тихо отдаёт форму поиска.
* **`srv_num` всегда `1`.** Ротация, которую делают внешние скраперы,
  у КСОЮ бессмысленна: параметр игнорируется, здание у суда одно (§3).
* Лишние пустые поля формы безвредны, но бесполезны — не шлём.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from .directories import Cartoteka, Court
from .encoding import SUBMIT_VALUE, percent_encode

#: Экземпляр базы суда. У КСОЮ он всегда один — см. §3 грамматики.
SRV_NUM = "1"

#: Записей на странице перечня — константа движка, от параметров не зависит.
PAGE_SIZE = 25


class DateAxis(Enum):
    """Три независимые оси дат. За одно и то же окно они дают РАЗНЫЕ выборки
    (727 / 770 / 530 дел на неделю), это не варианты одного фильтра.

    `PUBLICATION` — ось инкрементального добега: под ней у каждой строки есть
    ссылка на текст акта, потому что дело попадает в выборку именно по факту
    публикации.
    """

    ENTRY = "entry"
    RESULT = "result"
    PUBLICATION = "publication"

    def field_names(self, cartoteka: Cartoteka) -> tuple[str, str]:
        """Имена полей «от» и «до» для этой оси у этой картотеки."""
        if self is DateAxis.ENTRY:
            return (
                f"{cartoteka.delo_table}__ENTRY_DATE1D",
                f"{cartoteka.delo_table}__ENTRY_DATE2D",
            )
        if self is DateAxis.RESULT:
            return (
                f"{cartoteka.delo_table}__RESULT_DATE1D",
                f"{cartoteka.delo_table}__RESULT_DATE2D",
            )
        # У документа префикс свой и короче, чем у дела: G3_DOCUMENT__ при G33_CASE__.
        return (
            f"{cartoteka.doc_prefix}PUBL_DATE1D",
            f"{cartoteka.doc_prefix}PUBL_DATE2D",
        )


def format_date(value: date) -> str:
    """Портал понимает только дд.мм.гггг."""
    return value.strftime("%d.%m.%Y")


def _query(pairs: list[tuple[str, str]]) -> str:
    return "&".join(f"{key}={value}" for key, value in pairs)


def listing_url(
    court: Court,
    cartoteka: Cartoteka,
    axis: DateAxis,
    date_from: date,
    date_to: date,
    page: int = 1,
) -> str:
    """URL страницы сплошного перечня дел за окно дат."""
    if page < 1:
        raise ValueError("страницы нумеруются с 1")
    field_from, field_to = axis.field_names(cartoteka)
    pairs = [
        ("name", "sud_delo"),
        ("srv_num", SRV_NUM),
        ("name_op", "r"),
        ("page", str(page)),
        ("delo_id", cartoteka.delo_id),
        ("case_type", "0"),
        ("new", cartoteka.new),
        ("delo_table", cartoteka.delo_table),
        (field_from, percent_encode(format_date(date_from))),
        (field_to, percent_encode(format_date(date_to))),
        ("Submit", SUBMIT_VALUE),
    ]
    return f"https://{court.domain}/modules.php?{_query(pairs)}"


def act_url(court: Court, number: str, delo_id: str, new: str, text_number: int = 1) -> str:
    """URL текста судебного акта.

    `delo_id`/`new` берутся ИЗ ССЫЛКИ в строке выдачи, а не из справочника:
    портал ставит там длинную пару (`2800001`), даже если запрос ушёл с короткой.

    `text_number` — порядковый номер акта внутри дела, с 1. Несколько актов
    на дело возможны, но редки; на деле с одним актом `text_number=2` отдаёт
    «ПУСТОЙ ДОКУМЕНТ» — это признак конца перебора, а не ошибка.
    """
    pairs = [
        ("name", "sud_delo"),
        ("srv_num", SRV_NUM),
        ("name_op", "doc"),
        ("number", number),
        ("delo_id", delo_id),
        ("new", new),
        ("text_number", str(text_number)),
    ]
    return f"https://{court.domain}/modules.php?{_query(pairs)}"


def card_url(court: Court, case_id: str, case_uid: str, delo_id: str, new: str) -> str:
    """URL карточки дела. Нужна только там, где важны движение, полный состав
    сторон и реквизиты нижестоящего суда — реквизиты и текст акта берутся
    из строки выдачи одним запросом."""
    pairs = [
        ("name", "sud_delo"),
        ("srv_num", SRV_NUM),
        ("name_op", "case"),
        ("case_id", case_id),
        ("case_uid", case_uid),
        ("new", new),
        ("delo_id", delo_id),
    ]
    return f"https://{court.domain}/modules.php?{_query(pairs)}"


def page_count(total: int) -> int:
    """Сколько страниц у выборки из `total` дел — контроль полноты обхода."""
    if total < 0:
        raise ValueError("отрицательное число дел")
    return -(-total // PAGE_SIZE)
