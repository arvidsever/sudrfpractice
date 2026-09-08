"""Проба паузы: сколько суд на самом деле держит адрес.

Платформа отказывает вёрсткой с кодом 200 и никогда не говорит, сколько
ждать: за сутки 07.09.2026 — 190 отказов, ни одного `429`, ни одного
`Retry-After`. Получасовая пауза выбрана в августе на глаз, и цена этой
догадки — 95 судо-часов простоя в сутки на четыре суда.

Проба спрашивает суд раз в три минуты одним запросом. Два свойства,
которые здесь и держатся: она снимает паузу, если суд ответил, и не имеет
права её продлить, если нет.
"""

from __future__ import annotations

import threading
import time

from harvester import http
from harvester.http import _COOLDOWNS, cooldown_left, wait_out_cooldown


def _hold(domain: str, seconds: float) -> None:
    _COOLDOWNS[domain] = time.monotonic() + seconds


def test_answering_court_ends_the_pause_early(monkeypatch) -> None:
    """Ради этого всё и затевалось: суд ожил — ждать больше нечего."""
    monkeypatch.setattr(http, "PROBE_EVERY_SECONDS", 0.01)
    domain = "проба-ответил"
    _hold(domain, 3600)
    try:
        assert wait_out_cooldown(domain, threading.Event(), probe=lambda: True) is True
        assert cooldown_left(domain) == 0, "пауза снята"
    finally:
        _COOLDOWNS.pop(domain, None)


def test_silent_court_is_still_waited_out(monkeypatch) -> None:
    """Не ответил — ждём дальше, и проба паузу не продлевает."""
    monkeypatch.setattr(http, "PROBE_EVERY_SECONDS", 0.05)
    domain = "проба-молчит"
    _hold(domain, 1.0)
    probes: list[int] = []
    try:
        assert wait_out_cooldown(domain, threading.Event(), probe=lambda: probes.append(1) or False)
        assert probes, "стучаться надо было"
        assert cooldown_left(domain) == 0, "дождались своего, а не получили новую паузу"
    finally:
        _COOLDOWNS.pop(domain, None)


def test_broken_probe_does_not_break_waiting(monkeypatch) -> None:
    """Сеть у пробы своя, и её отказ — не повод ронять ожидание."""
    monkeypatch.setattr(http, "PROBE_EVERY_SECONDS", 0.05)
    domain = "проба-падает"
    _hold(domain, 1.0)

    def angry() -> bool:
        raise RuntimeError("суд не отвечает")

    try:
        assert wait_out_cooldown(domain, threading.Event(), probe=angry) is True
    finally:
        _COOLDOWNS.pop(domain, None)


def test_probe_asks_without_arming_the_back_off() -> None:
    """Проба обязана идти с `arm_back_off=False` и `ignore_cooldown=True`:
    иначе отказ на пробе поставил бы новую получасовую паузу, и стук по
    суду превратился бы в вечное ожидание."""
    from harvester import cards

    seen: dict[str, object] = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            seen.update(kwargs)
            return http.Response(url=url, status_code=200, content=b"<table id=tablcont></table>")

    import harvester.cards as cards_module

    original = cards_module.open_client
    cards_module.open_client = lambda *a, **k: FakeClient()
    try:
        probe = cards.make_probe("5kas.sudrf.ru")
        probe()
    except Exception:
        pass
    finally:
        cards_module.open_client = original

    assert seen.get("arm_back_off") is False
    assert seen.get("ignore_cooldown") is True
