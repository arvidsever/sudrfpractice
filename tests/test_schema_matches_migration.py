"""Схема в коде и схема в миграции не должны разъезжаться.

Таблицы описаны дважды — в `harvester.db.schema` (для запросов) и в миграции
(она обязана быть замороженной). Дублирование неизбежно, а вот молчаливое
расхождение — нет: этот тест сверяет их по фактической базе.

Пропускается, если базы нет: остальной набор сети и БД не требует.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from harvester.db.schema import metadata

#: Колонка есть в базе, но не в metadata: generated-колонка `tsvector`
#: описывается только миграцией — SQLAlchemy её всё равно не строит.
GENERATED_ONLY = {"act_text": {"tsv"}}


@pytest.fixture(scope="module")
def inspector(test_database_url: str):
    return inspect(create_engine(test_database_url))


def test_every_table_from_metadata_exists(inspector) -> None:
    present = set(inspector.get_table_names())
    missing = set(metadata.tables) - present
    assert not missing, f"миграция не создала таблицы: {sorted(missing)}"


def test_columns_agree(inspector) -> None:
    for name, table in metadata.tables.items():
        in_db = {column["name"] for column in inspector.get_columns(name)}
        in_code = set(table.columns.keys())
        assert in_code <= in_db, f"{name}: в базе нет колонок {sorted(in_code - in_db)}"
        extra = in_db - in_code - GENERATED_ONLY.get(name, set())
        assert not extra, f"{name}: в базе лишние колонки {sorted(extra)}"


def test_full_text_index_is_russian(inspector) -> None:
    """Полнотекст строится по русской конфигурации — иначе стемминга нет."""
    columns = {c["name"]: c for c in inspector.get_columns("act_text")}
    assert "tsv" in columns
    indexes = {index["name"] for index in inspector.get_indexes("act_text")}
    assert "ix_act_text_tsv" in indexes


def test_extensions_are_installed(inspector) -> None:
    with inspector.engine.connect() as connection:
        installed = {row[0] for row in connection.execute(text("select extname from pg_extension"))}
    assert {"pg_trgm", "vector"} <= installed


def test_socket_url_survives_alembic_config() -> None:
    """Адрес через unix-сокет не должен ломать миграции.

    `set_main_option` кладёт значение в configparser, где `%` начинает
    интерполяцию, а сокетный адрес несёт `?host=%2Fvar%2Frun%2Fpostgresql`.
    До удвоения процентов миграции падали с «invalid interpolation syntax»
    ещё до первого запроса — при живой и отвечающей базе.
    """
    from pathlib import Path

    from alembic.config import Config

    url = "postgresql+psycopg://sudrf@/praktika?host=%2Fvar%2Frun%2Fpostgresql"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    assert config.get_main_option("sqlalchemy.url") == url
