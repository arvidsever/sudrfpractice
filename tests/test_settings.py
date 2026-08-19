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
