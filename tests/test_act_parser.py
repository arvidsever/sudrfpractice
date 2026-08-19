"""Текст акта — §4 грамматики.

Здесь проверяется экономика всего сбора: пока текст со страницы `name_op=doc`
совпадает с блоком карточки, на дело хватает ОДНОГО запроса вместо двух.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from harvester.parse.act import parse_act, parse_card_act


def test_act_text_matches_card_block(act_doc: str, case_card: str) -> None:
    act = parse_act(act_doc, number="18565938")
    assert act.text == parse_card_act(case_card, text_number=1)
    assert not act.is_empty_document


def test_text_is_not_taken_from_doccont(act_doc: str) -> None:
    """`div#doccont` — это только кнопка печати; ходить туда за текстом нельзя."""
    doccont = HTMLParser(act_doc).css_first("div#doccont")
    assert doccont is not None
    assert doccont.text().strip() == ""

    act = parse_act(act_doc, number="18565938")
    assert len(act.text) > 10_000


def test_act_text_is_depersonalised(act_doc: str) -> None:
    """262-ФЗ: ФИО в тексте заменены на ФИО1/ФИО2. Реквизиты берутся из строки
    выдачи — из текста их не восстановить, и рассчитывать на это нельзя."""
    act = parse_act(act_doc, number="18565938")
    assert "ФИО1" in act.text or "ФИО2" in act.text


def test_empty_document_marks_end_of_text_number_probing() -> None:
    from harvester.models import ActText

    probe = ActText(number="1", text_number=2, text="ПУСТОЙ ДОКУМЕНТ")
    assert probe.is_empty_document
