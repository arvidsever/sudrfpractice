"""Чередование адреса выхода — решение владельца, проверенное кодом.

Правило — в `docs/ethics.md`: адреса сервера чередуются на каждый запрос,
заблокированные перебираются, а когда заблокированы все — останавливаемся.
Здесь проверяется, что именно это и делает клиент, а не что он делает вид.
"""

from __future__ import annotations

import httpx
import pytest

from harvester import http as http_module
from harvester.config import Settings
from harvester.http import CourtClient, CourtOnCooldown

ADDRESSES = ("10.0.0.1", "10.0.0.2", "10.0.0.3")


def _fresh_state(monkeypatch, addresses: tuple[str, ...]) -> Settings:
    monkeypatch.setattr(http_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(http_module, "_SOURCE_INDEX", 0)
    monkeypatch.setattr(http_module, "_COOLDOWNS", {})
    monkeypatch.setattr(http_module, "_LAST_REQUEST_ANY", {})
    monkeypatch.setattr(http_module, "_LAST_REQUEST", {})
    monkeypatch.setattr(http_module, "_REQUESTS_TODAY", {})
    return Settings(source_addresses=addresses)


def _client(monkeypatch, answers: dict[str | None, int], addresses=ADDRESSES):
    """Клиент, чей ответ зависит от адреса выхода, и список испрошенных адресов."""
    settings = _fresh_state(monkeypatch, addresses)
    seen: list[str | None] = []

    def factory(source: str | None) -> httpx.Client:
        seen.append(source)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(answers.get(source, 429), request=request, content=b"body")

        return httpx.Client(transport=httpx.MockTransport(handler))

    return CourtClient(settings, bulk=False, client_factory=factory), seen


def test_blocked_address_moves_to_the_next(monkeypatch) -> None:
    client, seen = _client(monkeypatch, {"10.0.0.1": 429, "10.0.0.2": 200})
    response = client.get("https://5kas.sudrf.ru/modules.php")
    assert response.status_code == 200
    assert seen == ["10.0.0.1", "10.0.0.2"], "на отказ адреса клиент обязан сменить адрес"


def test_consecutive_requests_use_different_addresses(monkeypatch) -> None:
    """Адреса идут по кругу: на каждый адрес — втрое меньше запросов,
    а пауза между его запросами — втрое больше."""
    client, seen = _client(monkeypatch, {address: 200 for address in ADDRESSES})
    for _ in range(4):
        assert client.get("https://5kas.sudrf.ru/modules.php").status_code == 200
    assert seen == ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.1"], "круг адресов"


def test_all_addresses_blocked_stops_the_run(monkeypatch) -> None:
    client, seen = _client(monkeypatch, {})  # все ответы 429
    with pytest.raises(CourtOnCooldown):
        client.get("https://5kas.sudrf.ru/modules.php")
    assert seen == list(ADDRESSES), "перебираются все адреса ровно один раз"
    assert client.cooldown_left("5kas.sudrf.ru") > 0


def test_locked_page_also_moves_to_the_next_address(monkeypatch) -> None:
    """«Доступ заблокирован» (КАД) — отказ адресу: клиент его возвращает,
    лишь перебрав все адреса, и уж тогда решает вызывающий."""
    client, seen = _client(monkeypatch, {"10.0.0.1": 451, "10.0.0.2": 451, "10.0.0.3": 451})
    response = client.get("https://kad.arbitr.ru/Kad/SearchInstances")
    assert response.status_code == 451
    assert seen == list(ADDRESSES)


def test_without_addresses_behaviour_is_unchanged(monkeypatch) -> None:
    """Пустой список — прежнее поведение: отступаем сразу, никуда не переходя."""
    client, seen = _client(monkeypatch, {}, addresses=())
    with pytest.raises(CourtOnCooldown):
        client.get("https://5kas.sudrf.ru/modules.php")
    assert seen == [None]


def test_foreign_client_is_not_silently_replaced(monkeypatch) -> None:
    """Чужой клиент пересобрать нечем — смены адреса нет: подмена потеряла бы
    его заголовки и туннель (КС РФ)."""
    settings = _fresh_state(monkeypatch, ADDRESSES)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request, content=b"body")

    client = CourtClient(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(CourtOnCooldown):
        client.get("https://5kas.sudrf.ru/modules.php")
    assert http_module.active_source(ADDRESSES) == ADDRESSES[0], "адрес не сменился"
