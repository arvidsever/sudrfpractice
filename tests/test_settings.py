"""Правила уважительного доступа — это настройки, но не «просто настройки»."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harvester.config import Settings


def test_user_agent_carries_a_contact() -> None:
    """С дефолтным `curl/…` WAF отдаёт 403; контакт в UA — намеренно,
    чтобы суд мог связаться, а не только заблокировать."""
    agent = Settings().user_agent
    assert "curl" not in agent.lower()
    assert "@" in agent
    # HTTP-заголовки кодируются latin-1: кириллица в UA роняет запрос
    # ещё до отправки, а выглядит как сетевая ошибка.
    agent.encode("latin-1")


def test_throttle_cannot_be_set_below_three_seconds() -> None:
    """Дроссель — условие, на котором проект ходит на суды, а не тюнинг."""
    with pytest.raises(ValidationError):
        Settings(min_delay_seconds=0.5)


def test_dsn_uses_psycopg3_driver() -> None:
    assert Settings().database_url.startswith("postgresql+psycopg://")


def test_night_window_may_cross_midnight() -> None:
    """Окно задаётся часами [от, до) и может пересекать полночь."""
    from datetime import datetime

    from harvester.http import within_night_window

    assert within_night_window((1, 7), datetime(2026, 6, 1, 3))
    assert not within_night_window((1, 7), datetime(2026, 6, 1, 12))
    # Окно через полночь: 23–5.
    assert within_night_window((23, 5), datetime(2026, 6, 1, 23))
    assert within_night_window((23, 5), datetime(2026, 6, 1, 2))
    assert not within_night_window((23, 5), datetime(2026, 6, 1, 12))


def test_bulk_client_refuses_outside_the_night_window(monkeypatch) -> None:
    """Ночное окно — обещание из docs/ethics.md; проверяет его код, а не совесть.
    Единичный диагностический запрос под правило не подпадает: иначе проверить
    суд днём стало бы невозможно."""
    import pytest as _pytest

    from harvester import http as http_module

    monkeypatch.setattr(http_module, "within_night_window", lambda *_: False)

    bulk = http_module.CourtClient(bulk=True)
    with _pytest.raises(http_module.OutsideCollectionWindow):
        bulk.get("https://2kas.sudrf.ru/modules.php")
    bulk.close()


def test_open_window_lets_every_hour_through() -> None:
    """Круглосуточный сбор — это (0, 24), а не отсутствие проверки.

    Ограничение осталось настройкой: вернуть окно значит поправить одну
    строку, а не восстанавливать выброшенный механизм.
    """
    from datetime import datetime

    from harvester.http import within_night_window

    for hour in (0, 6, 12, 18, 23):
        assert within_night_window((0, 24), datetime(2026, 8, 20, hour, 30))


def test_too_many_requests_is_a_back_off_not_a_strange_page(monkeypatch) -> None:
    """429 — тот же антибрутфорс, что и «Информация временно недоступна».

    Он приходит статусом, а не вёрсткой, поэтому мимо обеих защит:
    отступление ищет русскую фразу, повтор срабатывает от 5xx. 20.08.2026
    из-за этого умерло восемнадцать окон подряд: страница не опознавалась,
    а следующий запрос уходил через те же три секунды.
    """
    import httpx

    from harvester import http as http_module
    from harvester.http import CourtClient, CourtOnCooldown

    request = httpx.Request("GET", "https://5kas.sudrf.ru/modules.php")
    too_many = httpx.Response(
        429,
        request=request,
        content=b"<html><head><title>429 Too Many Requests</title></head></html>",
        headers={"Retry-After": "120"},
    )

    client = CourtClient(
        bulk=False, client=httpx.Client(transport=httpx.MockTransport(lambda _: too_many))
    )
    monkeypatch.setattr(http_module, "_LAST_REQUEST_ANY", [0.0])

    try:
        with pytest.raises(CourtOnCooldown):
            client.get("https://5kas.sudrf.ru/modules.php")
        left = client.cooldown_left("5kas.sudrf.ru")
    finally:
        http_module._COOLDOWNS.clear()

    assert left > 120, "просьба сервера подождать две минуты нас не торопит: отступаем на своё"


def test_global_throttle_holds_across_courts(monkeypatch) -> None:
    """Дроссель на хост не ограничивает платформу: десять судов дают десять
    запросов за те же три секунды. Общая пауза считается от последнего
    запроса к ЛЮБОМУ суду.
    """
    import httpx

    from harvester import http as http_module
    from harvester.http import CourtClient

    slept: list[float] = []
    monkeypatch.setattr(http_module.time, "sleep", slept.append)
    monkeypatch.setattr(http_module, "_LAST_REQUEST_ANY", [0.0])
    monkeypatch.setattr(http_module, "_LAST_REQUEST", {})

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"<html></html>")

    client = CourtClient(bulk=False, client=httpx.Client(transport=httpx.MockTransport(ok)))

    client.get("https://1kas.sudrf.ru/a")
    slept.clear()
    client.get("https://2kas.sudrf.ru/b")  # другой суд — пауза на хост не при чём

    assert slept, "второй запрос к ДРУГОМУ суду обязан выждать общую паузу"
    assert max(slept) <= client.settings.global_min_delay_seconds


def test_daily_cap_rolls_over_at_midnight(monkeypatch) -> None:
    """Потолок обязан отпускать назавтра.

    Пока обход запускался на ночь, процесс умирал каждое утро и счётчик
    обнулялся заодно. Круглосуточный процесс живёт сутками: без даты
    в ключе взятый однажды потолок не отпустил бы никогда, и все окна
    суда посыпались бы в `DailyCapReached`, сжигая попытки.
    """
    from datetime import date

    import httpx

    from harvester import http as http_module
    from harvester.config import Settings
    from harvester.http import CourtClient, DailyCapReached

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"<html></html>")

    monkeypatch.setattr(http_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(http_module, "_LAST_REQUEST_ANY", [0.0])
    monkeypatch.setattr(http_module, "_LAST_REQUEST", {})
    monkeypatch.setattr(http_module, "_REQUESTS_TODAY", {})
    monkeypatch.setattr(http_module, "_today", lambda: date(2026, 8, 20))

    settings = Settings(daily_request_cap=2)
    client = CourtClient(
        settings=settings, bulk=False, client=httpx.Client(transport=httpx.MockTransport(ok))
    )

    client.get("https://5kas.sudrf.ru/a")
    client.get("https://5kas.sudrf.ru/b")
    with pytest.raises(DailyCapReached):
        client.get("https://5kas.sudrf.ru/c")

    monkeypatch.setattr(http_module, "_today", lambda: date(2026, 8, 21))
    client.get("https://5kas.sudrf.ru/d")  # назавтра потолок отпустил

    assert client.requests_today == {"5kas.sudrf.ru": 1}, "счётчик показывает сегодняшний день"
