"""Разбор строки перечня — §4 грамматики.

Значения сверены с регрессией `KSOYuListingGrammarTests` в репозитории `Sudrf`:
два независимых разбора одной и той же живой страницы должны сходиться.
"""

from __future__ import annotations

import pytest

from harvester.parse.listing import COLUMN_COUNT, column_titles, parse_listing

DOMAIN = "2kas.sudrf.ru"


def test_page_size_is_twenty_five(listing_acts: str) -> None:
    """25 записей на страницу — константа движка, от параметров запроса не зависит."""
    page = parse_listing(listing_acts, court_domain=DOMAIN, cartoteka_id="g3")
    assert len(page.rows) == 25
    assert page.total == 530


def test_eight_columns_with_acts_last(listing_acts: str) -> None:
    titles = column_titles(listing_acts)
    assert len(titles) == COLUMN_COUNT
    assert titles[0] == "№ дела"
    # Ради последней колонки харвестеру хватает одного запроса на дело вместо двух.
    assert titles[-1] == "Судебные акты"


def test_row_is_self_sufficient(listing_acts: str) -> None:
    """Номер, дата поступления, категория, судья, дата решения и результат
    берутся из строки — карточку открывать не нужно."""
    page = parse_listing(listing_acts, court_domain=DOMAIN, cartoteka_id="g3")
    first = page.rows[0]

    assert first.case_number == "8Г-15211/2026 [88-14715/2026]"
    assert first.case_id == "18223875"
    assert first.case_uid == "3128af6a-aafd-43ab-a873-18c4f46b860e"
    assert first.receipt_date == "28.04.2026"
    assert first.judge == "Попова Елена Викторовна"
    assert first.decision_date == "14.05.2026"
    assert first.result is not None and "ОСТАВЛЕНО БЕЗ УДОВЛЕТВОРЕНИЯ" in first.result
    assert first.essence is not None and "КАТЕГОРИЯ" in first.essence
    assert first.card_url is not None and first.card_url.startswith(f"https://{DOMAIN}/")


def test_publication_window_guarantees_act_links(listing_acts: str) -> None:
    """Под фильтром по дате публикации ссылка на текст есть у КАЖДОЙ строки:
    дело попадает в такую выборку именно потому, что акт уже опубликован."""
    page = parse_listing(listing_acts, court_domain=DOMAIN, cartoteka_id="g3")
    assert all(len(row.act_links) == 1 for row in page.rows)

    link = page.rows[0].act_links[0]
    assert link.number == "18565938"
    assert link.text_number == 1
    assert link.kind == "Постановления"
    # Портал ставит в ссылке ДЛИННУЮ пару, хотя запрос мог уйти с короткой.
    assert "&delo_id=2800001&new=2800001&" in link.url
    assert link.url.startswith(f"https://{DOMAIN}/modules.php?")


def test_missing_act_is_a_legal_state_not_a_parse_failure(listing_koap: str) -> None:
    """262-ФЗ: публикуется не всё. Пустой список актов — законное состояние."""
    page = parse_listing(listing_koap, court_domain=DOMAIN, cartoteka_id="adm3")
    assert page.rows
    assert any(not row.act_links for row in page.rows)
    assert any(row.act_links for row in page.rows)
    assert all(link.number for row in page.rows for link in row.act_links)


def test_parsing_a_non_listing_raises(listing_bad_new: str, listing_captcha_gate: str) -> None:
    """Разбирать не-выдачу нельзя: иначе кривой запрос молча станет «дел нет»."""
    for html in (listing_bad_new, listing_captcha_gate):
        with pytest.raises(ValueError, match="не является выдачей"):
            parse_listing(html, court_domain=DOMAIN, cartoteka_id="g3")


def test_listing_with_short_delo_id_still_parses(listing_appeal_delo_id: str) -> None:
    """С коротким `delo_id` портал не наполняет колонку с актами.

    Разбор от этого не ломается: счётчик, строки и реквизиты те же.
    Ловится это не парсером, а детектором `suspect_wrong_delo_id`.
    """
    page = parse_listing(listing_appeal_delo_id, court_domain=DOMAIN, cartoteka_id="g3")

    assert page.total == 530
    assert len(page.rows) == 25
    assert all(not row.act_links for row in page.rows)

    # Реквизиты не пострадали — то же дело, что и в фикстуре от 06.08.2026.
    first = page.rows[0]
    assert first.case_number == "8Г-15211/2026 [88-14715/2026]"
    assert first.judge == "Попова Елена Викторовна"
    assert first.case_uid == "3128af6a-aafd-43ab-a873-18c4f46b860e"


def test_wrong_delo_id_is_detected(listing_acts: str, listing_appeal_delo_id: str) -> None:
    """Полная страница без единой ссылки под осью публикации — признак
    короткого `delo_id`, а не отсутствия опубликованных актов."""
    from harvester.guards import listing_kind, suspect_wrong_delo_id

    good = parse_listing(listing_acts, court_domain=DOMAIN, cartoteka_id="g3")
    bad = parse_listing(listing_appeal_delo_id, court_domain=DOMAIN, cartoteka_id="g3")

    assert not suspect_wrong_delo_id(sum(1 for row in good.rows if row.act_links), len(good.rows))
    assert suspect_wrong_delo_id(sum(1 for row in bad.rows if row.act_links), len(bad.rows))
    # Пустая страница подозрительной не считается — там нечего наполнять.
    assert not suspect_wrong_delo_id(0, 0)

    # Прямой признак подмены: тот же счётчик, но другое производство.
    assert listing_kind(listing_acts) == "кассация"
    assert listing_kind(listing_appeal_delo_id) == "апелляция"
    assert good.total == bad.total == 530
