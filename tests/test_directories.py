"""Справочники: выгрузка из Swift плюс то, что знает только харвестер."""

from __future__ import annotations

import pytest

from harvester.directories import CAPTCHA_COURTS, cartoteka, cartoteki, cartoteki_for, court, courts


def test_nine_cassation_courts_plus_military() -> None:
    domains = {item.domain for item in courts() if item.level == "cassation"}
    assert domains == {f"{n}kas.sudrf.ru" for n in range(1, 10)} | {"vkas.sudrf.ru"}


def test_five_appeal_courts_plus_military() -> None:
    domains = {item.domain for item in courts() if item.level == "appeal"}
    assert domains == {f"{n}ap.sudrf.ru" for n in range(1, 6)} | {"vap.sudrf.ru"}
    assert court("vap.sudrf.ru").number is None


def test_military_court_has_no_number_and_no_regions() -> None:
    """Кассационный военный суд один на страну: территориальной подсудности
    по субъектам у него нет."""
    military = court("vkas.sudrf.ru")
    assert military.number is None
    assert military.regions == ()


def test_captcha_courts_match_the_live_survey() -> None:
    """КСОЮ проверены 06.08.2026, АСОЮ — 03.10.2026, по полю `captcha` в форме;
    суды субъектов — по воротам капчи в первую ночь индекса, 16–17.10.2026."""
    assert {
        "1kas.sudrf.ru",
        "3kas.sudrf.ru",
        "4kas.sudrf.ru",
        "6kas.sudrf.ru",
        "2ap.sudrf.ru",
        "vap.sudrf.ru",
    } == {c for c in CAPTCHA_COURTS if court(c).level != "subject"}
    assert sum(court(c).level == "subject" for c in CAPTCHA_COURTS) == 16
    assert court("2kas.sudrf.ru").has_captcha is False
    assert court("3kas.sudrf.ru").has_captcha is True


def test_four_cassation_cartoteki() -> None:
    """КАС и КоАП — РАЗНЫЕ картотеки, а не варианты одной; корпусу нужны обе."""
    ids = {item.id for item in cartoteki_for(court("1kas.sudrf.ru"))}
    assert ids == {"g3", "u3", "p3", "adm3"}
    assert cartoteka("p3").delo_table == "p33_case"
    assert cartoteka("adm3").delo_table == "adm33_case"


def test_civil_and_criminal_carry_mandatory_new() -> None:
    assert cartoteka("g3").new == "2800001"
    assert cartoteka("u3").new == "2450001"


def test_unknown_cartoteka_names_the_known_ones() -> None:
    with pytest.raises(KeyError, match="известны"):
        cartoteka("g1")


def test_each_court_gets_only_cartoteki_of_its_level() -> None:
    """Кассационная картотека у апелляционного суда — запрос, на который
    портал ответит формой или пустотой, и недосбор промолчит. Все переборы
    «суд × картотека» обязаны идти через `cartoteki_for`."""
    assert {c.id for c in cartoteki_for(court("1ap.sudrf.ru"))} == {"g2", "u2", "p2"}
    assert len(cartoteki()) == 15  # 4 кассационные, 3 апелляционные, 8 судов субъектов


def test_appeal_listing_uses_the_courts_own_short_pair() -> None:
    """У КСОЮ короткая пара лишала выдачу ссылок на акты. У АСОЮ она родная:
    её ставят в форму сами суды, и ссылки по оси публикации приходят."""
    assert cartoteka("g2").listing_delo_id == "5"
    assert cartoteka("u2").listing_delo_id == "4"
    assert cartoteka("p2").listing_delo_id == "42"
    assert cartoteka("p2").doc_prefix == "P2_DOCUMENT__"


def test_regional_courts_are_listed_but_not_harvested_until_enabled(monkeypatch) -> None:
    """87 судов субъектов есть в справочнике — по ним строятся адреса и ищется
    суд, — но обход по ним включается отдельно: под них нужен диск."""
    from harvester.config import settings
    from harvester.directories import cartoteki_for, court, courts, harvested_courts

    regional = court("oblsud.kln.sudrf.ru")
    assert regional.level == "subject"
    assert {k.id for k in cartoteki_for(regional)} == {
        "u1s",
        "u2s",
        "g1s",
        "g2s",
        "p1s",
        "p2s",
        "adm1s",
        "adm2s",
    }
    assert all(k.doc_prefix for k in cartoteki_for(regional))
    assert sum(c.level == "subject" for c in courts()) == 87
    assert regional not in harvested_courts()

    monkeypatch.setattr(settings, "harvest_levels", ("cassation", "appeal", "subject"))
    assert regional in harvested_courts()
