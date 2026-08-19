"""Сборка URL — §1 грамматики.

Эталон взят из документа грамматики и из фикстуры `ksoyu_listing_acts`:
это тот самый запрос, который живьём вернул 530 дел.
"""

from __future__ import annotations

from datetime import date

import pytest

from harvester.directories import cartoteka, court
from harvester.encoding import SUBMIT_VALUE, decode, percent_encode
from harvester.urls import DateAxis, listing_url, page_count

WINDOW = (date(2026, 6, 1), date(2026, 6, 7))


def _url(cartoteka_id: str = "g3", axis: DateAxis = DateAxis.ENTRY, page: int = 1) -> str:
    return listing_url(court("2kas.sudrf.ru"), cartoteka(cartoteka_id), axis, *WINDOW, page=page)


def test_entry_axis_matches_grammar_example() -> None:
    assert _url() == (
        "https://2kas.sudrf.ru/modules.php?name=sud_delo&srv_num=1&name_op=r&page=1"
        "&delo_id=2800001&case_type=0&new=2800001&delo_table=g33_case"
        "&g33_case__ENTRY_DATE1D=01.06.2026&g33_case__ENTRY_DATE2D=07.06.2026"
        f"&Submit={SUBMIT_VALUE}"
    )


def test_vnkod_is_never_emitted() -> None:
    """Полный набор пустых полей из `sudrfscraper` ломает выдачу именно
    из-за `&vnkod=`: сервер отвечает 200 и формой поиска."""
    for cartoteka_id in ("g3", "u3", "p3", "adm3"):
        for axis in DateAxis:
            assert "vnkod" not in _url(cartoteka_id, axis).lower()


def test_listing_uses_the_long_delo_id() -> None:
    """С коротким `delo_id` портал отдаёт тот же счётчик и те же дела,
    но озаглавливает выдачу «апелляция» и не даёт ссылок на тексты актов.
    Счётчик при этом сходится, поэтому ошибка молчит."""
    assert "&delo_id=2800001&" in _url("g3")
    assert "&delo_id=2450001&" in _url("u3")
    assert "&delo_id=43&" in _url("p3")
    assert "&delo_id=2550001&" in _url("adm3")
    # Реестр из Swift хранит короткие пары — для карточки они верны.
    assert cartoteka("g3").delo_id == "5"
    assert cartoteka("g3").listing_delo_id == "2800001"


def test_new_is_always_sent() -> None:
    """`g33_case`/`u33_case` с `new=0` или без `new` тихо отдают форму поиска."""
    for cartoteka_id in ("g3", "u3", "p3", "adm3"):
        assert "&new=" in _url(cartoteka_id)
    assert "&new=2800001&" in _url("g3")
    assert "&new=2450001&" in _url("u3")


def test_submit_is_cp1251_encoded() -> None:
    """Без «Найти» выдачи нет вовсе, и значение уходит в cp1251, не в UTF-8."""
    assert percent_encode("Найти") == SUBMIT_VALUE
    assert _url().endswith(f"&Submit={SUBMIT_VALUE}")


def test_srv_num_is_pinned_to_one() -> None:
    """У КСОЮ ротация `srv_num` бессмысленна: параметр игнорируется (§3)."""
    assert "&srv_num=1&" in _url()


def test_three_date_axes_use_different_fields() -> None:
    """Три оси независимы и дают РАЗНЫЕ выборки за одно окно — это разные
    фильтры, а не варианты одного."""
    assert "g33_case__ENTRY_DATE1D=" in _url(axis=DateAxis.ENTRY)
    assert "g33_case__RESULT_DATE1D=" in _url(axis=DateAxis.RESULT)
    assert "G3_DOCUMENT__PUBL_DATE1D=" in _url(axis=DateAxis.PUBLICATION)


def test_document_prefix_is_per_cartoteka() -> None:
    """Префикс документа короче префикса дела и свой у каждой картотеки;
    подстановка чужого выдачи не даёт."""
    expected = {
        "g3": "G3_DOCUMENT__",
        "u3": "U3_DOCUMENT__",
        "p3": "P3_DOCUMENT__",
        "adm3": "ADM3_DOCUMENT__",
    }
    for cartoteka_id, prefix in expected.items():
        assert f"{prefix}PUBL_DATE1D=" in _url(cartoteka_id, DateAxis.PUBLICATION)


def test_pagination_starts_at_one() -> None:
    assert "&page=3&" in _url(page=3)
    with pytest.raises(ValueError):
        _url(page=0)


def test_page_count_follows_the_counter() -> None:
    """`ceil(N / 25)` — контроль полноты обхода: 530 дел живьём дали 22 страницы,
    последняя из них неполная."""
    assert page_count(530) == 22
    assert page_count(25) == 1
    assert page_count(26) == 2
    assert page_count(0) == 0


def test_cyrillic_values_go_out_in_cp1251() -> None:
    """Кириллица кодируется в windows-1251; UTF-8 здесь не ошибка формата,
    а тихий промах — сервер ответит 200 и пустой выдачей."""
    assert percent_encode("Иванов") == "%C8%E2%E0%ED%EE%E2"
    assert decode("Найти".encode("cp1251")) == "Найти"
