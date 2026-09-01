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


def test_empty_pass_does_not_kill_the_thread_at_once(monkeypatch) -> None:
    """Один пустой заход — не «дел нет», а чаще «суду сейчас нехорошо».

    31.08.2026 суды несколько часов отвечали таймаутами; заход в 200
    карточек не дал ни одной, и потоки 1, 2, 4 и 7 КСОЮ вышли навсегда —
    поднять их мог только новый процесс, а он не запускался. К утру
    из семи судов работали три, темп упал с 2 320 запросов в час до 105.
    """
    calls: list[str] = []
    scripted = [
        _result("1kas.sudrf.ru", collected=0, remaining=99),
        _result("1kas.sudrf.ru", collected=0, remaining=99),
        _result("1kas.sudrf.ru", collected=7, remaining=0),
    ]

    def fake(domain, **_):
        calls.append(domain)
        return scripted.pop(0)

    monkeypatch.setattr(cards, "collect_cards", fake)
    monkeypatch.setattr(cards, "EMPTY_ROUND_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr("harvester.directories.courts", lambda: [_court("1kas.sudrf.ru")])

    results = cards.sweep_all()

    assert len(calls) == 3, "после пустого захода суду дают ещё попытку"
    assert results[0].cards == 7


def test_thread_leaves_after_enough_empty_rounds(monkeypatch) -> None:
    """Но и крутиться вхолостую нельзя: суд может молчать всерьёз."""
    calls: list[str] = []

    def fake(domain, **_):
        calls.append(domain)
        return _result(domain, collected=0, remaining=99)

    monkeypatch.setattr(cards, "collect_cards", fake)
    monkeypatch.setattr(cards, "EMPTY_ROUND_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr("harvester.directories.courts", lambda: [_court("1kas.sudrf.ru")])

    cards.sweep_all()

    assert len(calls) == cards.EMPTY_ROUNDS_BEFORE_LEAVING


def test_run_is_bounded_in_time(monkeypatch) -> None:
    """Свод обязан иногда отпускать замок, иначе суточный добег его
    не дождётся: 01.09.2026 добег простоял в очереди десять часов, потому
    что `RuntimeMaxSec` к `Type=oneshot` не применяется и молча ничего
    не делал. Ограничение живёт в коде, а не в systemd."""
    calls: list[str] = []

    def fake(domain, **_):
        calls.append(domain)
        return _result(domain, collected=1, remaining=99)

    monkeypatch.setattr(cards, "collect_cards", fake)
    monkeypatch.setattr("harvester.directories.courts", lambda: [_court("1kas.sudrf.ru")])

    cards.sweep_all(max_hours=0)

    assert calls == [], "срок вышел до первого захода — не начинаем"


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
