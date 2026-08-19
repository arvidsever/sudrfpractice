"""Классификация ответа — §5 грамматики.

Главное, что здесь проверяется: харвестер НЕ выдаёт кривой запрос
за пустую выдачу и не разбирает не-выдачу.
"""

from __future__ import annotations

import pytest

from harvester.guards import (
    CompletenessError,
    PageState,
    Verdict,
    check_page_completeness,
    classify,
)


def test_listing_gives_counter_and_range(listing_acts: str) -> None:
    state = classify(listing_acts)
    assert state.verdict is Verdict.LISTING
    # Счётчик — единственный источник общего числа дел: больше нигде
    # в разметке эта величина не дублируется.
    assert state.total == 530
    assert state.shown_range == (1, 25)


def test_koap_listing_is_also_a_listing(listing_koap: str) -> None:
    state = classify(listing_koap)
    assert state.verdict is Verdict.LISTING
    assert state.total == 143


def test_malformed_request_is_not_mistaken_for_a_listing(listing_bad_new: str) -> None:
    """`g33_case` с `new=0` — 200 OK и «Данных по запросу не обнаружено».

    Отличить его от честной пустоты по тексту нечем (§5), поэтому вердикт
    один на оба случая и он ЯВНО помечен как требующий подтверждения.
    Важно другое: таблицы нет, значит разбирать нечего и «нашли 0 дел»
    из этого ответа не следует.
    """
    state = classify(listing_bad_new)
    assert state.verdict is Verdict.NO_DATA
    assert state.total is None
    assert state.needs_confirmation
    assert not state.is_listing


def test_captcha_gate_says_so_in_plain_text(listing_captcha_gate: str) -> None:
    """Капча-суды ведут себя лучше пустоты: отсутствующий код сервер трактует
    как неверный и говорит об этом прямым текстом в `div#error`."""
    state = classify(listing_captcha_gate)
    assert state.verdict is Verdict.CAPTCHA_GATE
    assert not state.is_listing


def test_unknown_page_is_not_silently_accepted() -> None:
    assert classify("<html><body>что-то новое</body></html>").verdict is Verdict.UNKNOWN


def test_completeness_accepts_full_page() -> None:
    check_page_completeness(PageState(Verdict.LISTING, total=530), parsed_rows=25, page=1)


def test_completeness_accepts_short_last_page() -> None:
    # 530 дел → 22 страницы, на последней 530 - 25 * 21 = 5 записей.
    check_page_completeness(PageState(Verdict.LISTING, total=530), parsed_rows=5, page=22)


def test_completeness_rejects_short_page() -> None:
    with pytest.raises(CompletenessError, match="разобрано 24"):
        check_page_completeness(PageState(Verdict.LISTING, total=530), parsed_rows=24, page=1)


def test_completeness_rejects_listing_without_counter() -> None:
    with pytest.raises(CompletenessError, match="нет счётчика"):
        check_page_completeness(PageState(Verdict.LISTING, total=None), parsed_rows=25, page=1)
