"""Куски актов и их эмбеддинги.

Тип `vector` объявлен прямо в SQL: обёртка `pgvector-python` ради одной
колонки и одного литерала не нужна.

`ON DELETE CASCADE` от акта: куски выводятся из текста и своей ценности
не имеют.

Индекс HNSW здесь не строится намеренно — на пустой таблице он бесполезен,
а на заполненной строится вдесятеро быстрее, чем достраивается по одному.
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE chunk (
            id        bigserial PRIMARY KEY,
            act_id    bigint NOT NULL REFERENCES act(id) ON DELETE CASCADE,
            ordinal   integer NOT NULL,
            text      text NOT NULL,
            embedding vector(1024),
            UNIQUE (act_id, ordinal)
        )
    """)
    # По нему ищется, что осталось посчитать.
    op.execute("CREATE INDEX ix_chunk_todo ON chunk (act_id) WHERE embedding IS NULL")


def downgrade() -> None:
    op.execute("DROP TABLE chunk")
