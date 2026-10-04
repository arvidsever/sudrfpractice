"""КС РФ: решения и их тексты (docs/ksrf.md).

Как у ВС: своими таблицами, полнотекст — generated-колонка `tsvector`
по русской конфигурации и GIN поверх, чтобы решения КС искались тем же
запросом, что тексты кассации и ВС.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO court (domain, number, title, level, regions, has_captcha) "
        "VALUES ('www.ksrf.ru', NULL, 'Конституционный Суд Российской Федерации', "
        "'constitutional', '[]', false) ON CONFLICT (domain) DO NOTHING"
    )
    op.create_table(
        "ksrf_decision",
        sa.Column("pdf_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("number", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("decision_date", sa.Date(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("text_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ksrf_decision_pending",
        "ksrf_decision",
        ["decision_date"],
        postgresql_where=sa.text("text_fetched_at IS NULL"),
    )
    op.create_table(
        "ksrf_decision_text",
        sa.Column(
            "pdf_id",
            sa.BigInteger(),
            sa.ForeignKey("ksrf_decision.pdf_id", ondelete="CASCADE"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column("raw_page_id", sa.BigInteger(), sa.ForeignKey("raw_page.id"), nullable=True),
        sa.Column("plain_text", sa.Text(), nullable=False),
    )
    op.execute(
        "ALTER TABLE ksrf_decision_text ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('russian', plain_text)) STORED"
    )
    op.execute("CREATE INDEX ix_ksrf_decision_text_tsv ON ksrf_decision_text USING gin (tsv)")


def downgrade() -> None:
    op.drop_table("ksrf_decision_text")
    op.drop_table("ksrf_decision")
    op.execute("DELETE FROM court WHERE domain = 'www.ksrf.ru'")
