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


def test_throttle_cannot_be_set_below_three_seconds() -> None:
    """Дроссель — условие, на котором проект ходит на суды, а не тюнинг."""
    with pytest.raises(ValidationError):
        Settings(min_delay_seconds=0.5)


def test_dsn_uses_psycopg3_driver() -> None:
    assert Settings().database_url.startswith("postgresql+psycopg://")
