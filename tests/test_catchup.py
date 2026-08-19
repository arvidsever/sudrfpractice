"""Инкрементальный добег."""

from __future__ import annotations

from datetime import date

import pytest

from harvester import catchup as catchup_module
from harvester.catchup import DEFAULT_LOOKBACK_DAYS, catchup
from harvester.http import CourtOnCooldown
from harvester.urls import DateAxis


@pytest.fixture
def calls(monkeypatch):
    seen = []

    class _Run:
        status, cases, acts, run_id, note = "complete", 7, 7, 1, None

    def fake(court, cartoteka, axis, start, end, **kwargs):
        seen.append((court.domain, cartoteka.id, axis, start, end))
        return _Run()

    monkeypatch.setattr(catchup_module, "harvest_listing", fake)
    return seen


def test_window_reaches_back_with_a_margin(calls) -> None:
    """Портал публикует и задним числом. Окно строго «за вчера» такие акты
    потеряло бы молча, поэтому запас назад — не перестраховка."""
    catchup(
        days=DEFAULT_LOOKBACK_DAYS,
        today=date(2026, 8, 19),
        only_courts=["5kas.sudrf.ru"],
        bulk=False,
    )

    starts = {start for *_, start, _ in calls}
    ends = {end for *_, end in calls}
    assert starts == {date(2026, 8, 16)}
    assert ends == {date(2026, 8, 19)}


def test_publication_axis_only(calls) -> None:
    """Акт публикуется много позже рассмотрения, поэтому ось поступления
    пришлось бы перечитывать целиком ради дозревших текстов."""
    catchup(days=1, today=date(2026, 8, 19), only_courts=["5kas.sudrf.ru"], bulk=False)
    assert {axis for _, _, axis, _, _ in calls} == {DateAxis.PUBLICATION}


def test_every_cartoteka_of_the_court(calls) -> None:
    catchup(days=1, today=date(2026, 8, 19), only_courts=["5kas.sudrf.ru"], bulk=False)
    assert {cartoteka for _, cartoteka, *_ in calls} == {"g3", "u3", "p3", "adm3"}


def test_paused_court_is_left_alone(monkeypatch) -> None:
    """Суд на паузе пропускается целиком: завтрашний добег с запасом назад
    поймает то же окно."""

    def refuse(court, cartoteka, axis, start, end, **kwargs):
        raise CourtOnCooldown(f"{court.domain}: пауза")

    monkeypatch.setattr(catchup_module, "harvest_listing", refuse)
    result = catchup(
        days=1, today=date(2026, 8, 19), only_courts=["5kas.sudrf.ru", "9kas.sudrf.ru"], bulk=False
    )

    # По одному пропуску на суд, а не по одному на картотеку.
    assert result.skipped == 2
    assert result.windows == 0


def test_one_broken_cartoteka_does_not_stop_the_rest(monkeypatch) -> None:
    calls = []

    class _Run:
        status, cases, acts, run_id, note = "complete", 3, 3, 1, None

    def sometimes(court, cartoteka, axis, start, end, **kwargs):
        calls.append(cartoteka.id)
        if cartoteka.id == "u3":
            raise RuntimeError("вёрстка поменялась")
        return _Run()

    monkeypatch.setattr(catchup_module, "harvest_listing", sometimes)
    result = catchup(days=1, today=date(2026, 8, 19), only_courts=["5kas.sudrf.ru"], bulk=False)

    assert len(calls) == 4
    assert result.failed == 1
    assert result.windows == 3
    assert any("u3" in problem for problem in result.problems)
