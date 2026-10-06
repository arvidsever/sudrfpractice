"""MCP-сервер: те же `search` и `act`, что в CLI, но для Claude вне мака."""

from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("mcp")


def test_tools_answer_with_cli_output(db_settings, monkeypatch) -> None:
    from sqlalchemy import create_engine, insert

    from harvester import config, mcp_server
    from harvester.db.schema import ksrf_decision, ksrf_decision_text

    monkeypatch.setattr(config.settings, "database_url", db_settings.database_url)
    engine = create_engine(db_settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            insert(ksrf_decision).values(
                pdf_id=7, number="57-П/2026", kind="П", decision_date=date(2026, 2, 1)
            )
        )
        connection.execute(
            insert(ksrf_decision_text).values(pdf_id=7, plain_text="О соразмерности неустойки.")
        )

    found = mcp_server.search(text="неустойка")
    assert "Конституционный Суд РФ: найдено 1" in found
    assert "KSRFDecision7.pdf" in found
    assert "О соразмерности неустойки." in mcp_server.act("57-П/2026")
    # Кривые параметры — ответ, а не падение сервера.
    assert "дд.мм.гггг" in mcp_server.search(text="неустойка", date_from="2026-01-01")
    assert "нужен text или number" in mcp_server.search()


def test_server_does_not_start_without_a_real_secret(monkeypatch) -> None:
    from harvester import mcp_server

    monkeypatch.setenv("SUDRF_MCP_SECRET", "short")
    monkeypatch.setenv("SUDRF_MCP_HOST", "example.test")
    with pytest.raises(RuntimeError):
        mcp_server.app()
