"""Классификация ответа портала — защита от тихого недосбора.

Главный риск проекта в том, что платформа отвечает `200 OK` на любой
некорректный запрос (§5 грамматики). Кривой запрос и честная пустота
**неотличимы по тексту**: оба дают `div#error` с «Данных по запросу
не обнаружено» и не дают таблицы. Поэтому:

* выдачей считается ТОЛЬКО ответ с `table#tablcont`;
* `NO_DATA` — заведомо неоднозначный вердикт, а не «дел нет». Разрешает
  его лишь контрольный запрос с заведомо непустым окном;
* число разобранных строк сверяется со счётчиком «Всего по запросу найдено».

Молчаливое «ничего не нашлось» здесь считается подозрением, а не фактом:
недосбор не проявляется как ошибка и иначе его нечем поймать.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from selectolax.parser import HTMLParser

_COUNTER_RE = re.compile(r"Всего по запросу найдено\s*[—-]\s*(\d+)")
_RANGE_RE = re.compile(r"На странице записи с\s*(\d+)\s+по\s*(\d+)", re.DOTALL)

_CAPTCHA_MARKER = "проверочный код"
_NO_DATA_MARKER = "Данных по запросу не обнаружено"


class Verdict(Enum):
    """Во что портал разрешил превратить ответ."""

    #: Есть `table#tablcont` — единственное состояние, из которого можно разбирать дела.
    LISTING = "listing"
    #: Есть текст «Данных по запросу не обнаружено». Честная пустота ИЛИ кривой
    #: запрос — различить нечем, см. §5. Всегда требует подтверждения.
    NO_DATA = "no_data"
    #: Суд с капчей не получил пары `captcha`+`captchaid` либо она не подошла.
    #: Отсутствующий код сервер трактует как неверный и говорит об этом прямо.
    CAPTCHA_GATE = "captcha_gate"
    #: Ответ вообще не похож ни на одно из известных состояний — новая вёрстка,
    #: страница WAF, ошибка платформы. Разбирать нельзя, нужна новая фикстура.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PageState:
    verdict: Verdict
    #: Счётчик «Всего по запросу найдено — N». None, если выдачи нет.
    total: int | None = None
    #: Диапазон записей на странице («с 1 по 25») — для сверки пагинации.
    shown_range: tuple[int, int] | None = None

    @property
    def is_listing(self) -> bool:
        return self.verdict is Verdict.LISTING

    @property
    def needs_confirmation(self) -> bool:
        """Пустая выдача подозрительна, пока её не подтвердил контрольный запрос."""
        return self.verdict is Verdict.NO_DATA


def classify(html: str) -> PageState:
    """Определить состояние страницы перечня."""
    tree = HTMLParser(html)

    if tree.css_first("table#tablcont") is not None:
        total_match = _COUNTER_RE.search(html)
        range_match = _RANGE_RE.search(html)
        return PageState(
            verdict=Verdict.LISTING,
            total=int(total_match.group(1)) if total_match else None,
            shown_range=(
                (int(range_match.group(1)), int(range_match.group(2))) if range_match else None
            ),
        )

    error = tree.css_first("#error")
    error_text = error.text() if error is not None else ""

    if _CAPTCHA_MARKER in error_text or _CAPTCHA_MARKER in html:
        return PageState(verdict=Verdict.CAPTCHA_GATE)
    if _NO_DATA_MARKER in error_text or _NO_DATA_MARKER in html:
        return PageState(verdict=Verdict.NO_DATA)
    return PageState(verdict=Verdict.UNKNOWN)


class CompletenessError(RuntimeError):
    """Разобрано не столько строк, сколько обещал счётчик."""


def check_page_completeness(state: PageState, parsed_rows: int, page: int) -> None:
    """Сверить число разобранных строк с обещанием счётчика.

    Без этой сверки страница с изменившейся вёрсткой выглядит как страница
    с меньшим числом дел, и корпус недосчитается молча.
    """
    if not state.is_listing:
        raise CompletenessError(f"страница {page}: не выдача ({state.verdict.value})")
    if state.total is None:
        raise CompletenessError(f"страница {page}: есть таблица, но нет счётчика")

    from .urls import PAGE_SIZE

    expected = min(PAGE_SIZE, max(0, state.total - PAGE_SIZE * (page - 1)))
    if parsed_rows != expected:
        raise CompletenessError(
            f"страница {page}: разобрано {parsed_rows} строк, ожидалось {expected} "
            f"(всего по счётчику {state.total})"
        )
