"""Справочники: выгрузка из Swift плюс то, что знает только харвестер."""

from __future__ import annotations

import pytest

from harvester.directories import CAPTCHA_COURTS, cartoteka, cartoteki, court, courts


def test_nine_cassation_courts_plus_military() -> None:
    assert len(courts()) == 10
    domains = {item.domain for item in courts()}
    assert domains == {f"{n}kas.sudrf.ru" for n in range(1, 10)} | {"vkas.sudrf.ru"}


def test_military_court_has_no_number_and_no_regions() -> None:
    """Кассационный военный суд один на страну: территориальной подсудности
    по субъектам у него нет."""
    military = court("vkas.sudrf.ru")
    assert military.number is None
    assert military.regions == ()


def test_captcha_courts_match_the_live_survey() -> None:
    """Проверено 06.08.2026 по наличию поля `captcha` в форме поиска (§6)."""
    assert {"1kas.sudrf.ru", "3kas.sudrf.ru", "4kas.sudrf.ru", "6kas.sudrf.ru"} == CAPTCHA_COURTS
    assert court("2kas.sudrf.ru").has_captcha is False
    assert court("3kas.sudrf.ru").has_captcha is True


def test_four_cassation_cartoteki() -> None:
    """КАС и КоАП — РАЗНЫЕ картотеки, а не варианты одной; корпусу нужны обе."""
    ids = {item.id for item in cartoteki()}
    assert ids == {"g3", "u3", "p3", "adm3"}
    assert cartoteka("p3").delo_table == "p33_case"
    assert cartoteka("adm3").delo_table == "adm33_case"


def test_civil_and_criminal_carry_mandatory_new() -> None:
    assert cartoteka("g3").new == "2800001"
    assert cartoteka("u3").new == "2450001"


def test_unknown_cartoteka_names_the_known_ones() -> None:
    with pytest.raises(KeyError, match="известны"):
        cartoteka("g1")
