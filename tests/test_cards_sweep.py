"""Обход всех судов: поток на суд, и чем он обязан кончиться.

Первая версия шла судами по очереди, и замер сразу это наказал: 3,0 с
между запросами, 1 200 в час. Упирался дроссель на хост, потому что
в каждый момент открыт был ровно один суд, а общий дроссель (1,5 с)
простаивал — ему нечего сдерживать, пока запросы и так идут вдвое реже.
Поток на суд делает главным общий дроссель, то есть потолок платформы.

Отсюда два свойства, которые тут и держатся: суд на паузе не бросают,
а досыпают её; заход без работы и без паузы прогон заканчивает.
"""

from __future__ import annotations

import time

from harvester import cards
from harvester.http import _COOLDOWNS


def _court(domain: str):
    return type("C", (), {"domain": domain})()


def _result(domain: str, *, collected: int, remaining: int, throttled: bool = False):
    return cards.CardSweepResult(
        court_domain=domain,
        attempted=collected,
        cards=collected,
        texts=collected,
        participants=0,
        without_text=0,
        failed=0,
        remaining=remaining,
        throttled=throttled,
    )


def test_court_on_pause_is_waited_out_not_dropped(monkeypatch) -> None:
    """Пауза длится полчаса, свод идёт сутками. Поток, ушедший от паузы,
    означал бы, что суд, однажды придержавший нас, больше не собирается
    никогда — так 20.08 к утру из десяти судов работал один."""
    calls: list[str] = []
    scripted = [
        _result("1kas.sudrf.ru", collected=0, remaining=5, throttled=True),
        _result("1kas.sudrf.ru", collected=5, remaining=0),
    ]

    def fake(domain, **_):
        calls.append(domain)
        result = scripted.pop(0)
        if result.throttled:
            # Настоящий клиент вместе с отказом ставит паузу; без неё
            # ждать было бы нечего.
            _COOLDOWNS[domain] = time.monotonic() + 0.2
        return result

    monkeypatch.setattr(cards, "collect_cards", fake)
    monkeypatch.setattr("harvester.directories.courts", lambda: [_court("1kas.sudrf.ru")])
    try:
        results = cards.sweep_all()
    finally:
        _COOLDOWNS.pop("1kas.sudrf.ru", None)

    assert calls == ["1kas.sudrf.ru"] * 2, "суд обязан быть взят снова после паузы"
    assert results[0].cards == 5


def test_empty_pass_without_a_pause_ends_the_run(monkeypatch) -> None:
    """Заход без карточек и без паузы повторялся бы вечно. Выходим,
    launchd поднимет через полчаса."""
    calls: list[str] = []

    def fake(domain, **_):
        calls.append(domain)
        return _result(domain, collected=0, remaining=99)

    monkeypatch.setattr(cards, "collect_cards", fake)
    monkeypatch.setattr(
        "harvester.directories.courts",
        lambda: [_court("1kas.sudrf.ru"), _court("2kas.sudrf.ru")],
    )

    cards.sweep_all()

    assert sorted(calls) == ["1kas.sudrf.ru", "2kas.sudrf.ru"], "по одному холостому заходу"


def test_every_court_gets_its_own_thread(monkeypatch) -> None:
    """Суды обязаны идти одновременно: иначе главным остаётся дроссель
    на хост, и темп вдвое ниже потолка платформы."""
    seen: set[str] = set()
    started = __import__("threading").Barrier(3, timeout=5)

    def fake(domain, **_):
        seen.add(__import__("threading").current_thread().name)
        started.wait()  # не пройдёт, если суды идут по очереди
        return _result(domain, collected=1, remaining=0)

    monkeypatch.setattr(cards, "collect_cards", fake)
    monkeypatch.setattr(
        "harvester.directories.courts",
        lambda: [_court("1kas.sudrf.ru"), _court("2kas.sudrf.ru"), _court("3kas.sudrf.ru")],
    )

    cards.sweep_all()

    assert len(seen) == 3, f"ожидались три потока, работали: {seen}"
