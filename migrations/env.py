"""Alembic: конфигурация окружения."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from harvester.config import settings
from harvester.db.schema import metadata

config = context.config

# URL из настроек — умолчание, а не приказ. Вызывающая сторона (тесты, скрипт
# раскатки) может задать свою базу через `config.attributes`, и переписывать
# её здесь значило бы накатить миграцию не туда.
#
# Проценты удваиваются: `set_main_option` кладёт значение в configparser,
# а там `%` начинает интерполяцию. Адрес через unix-сокет несёт
# `?host=%2Fvar%2Frun%2Fpostgresql` — и миграции падали на нём с
# «invalid interpolation syntax», хотя сама база отвечала. Наступлено
# при переезде на сервер, где сокет и peer-аутентификация естественнее
# пароля в открытом файле.
config.set_main_option(
    "sqlalchemy.url",
    (config.attributes.get("sqlalchemy_url") or settings.database_url).replace("%", "%%"),
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
