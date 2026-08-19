"""Начальная схема правовой базы КСОЮ

Revision ID: 0001
Revises:
Create Date: 2026-08-19

Расширения ставятся здесь же: `pg_trgm` — для поиска по номеру дела
с опечатками, `vector` — задел под семантический поиск. Колонки `embedding`
пока нет: `vector(N)` фиксирует размерность схемой, а модель ещё не выбрана.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "court",
        sa.Column("domain", sa.String(64), primary_key=True),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("regions", sa.Text(), nullable=False),
        sa.Column("has_captcha", sa.Boolean(), nullable=False, server_default="false"),
    )

    op.create_table(
        "cartoteka",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("delo_id", sa.String(16), nullable=False),
        sa.Column("new", sa.String(16), nullable=False),
        sa.Column("delo_table", sa.String(32), nullable=False),
        sa.Column("doc_prefix", sa.String(32), nullable=False),
    )

    op.create_table(
        "raw_page",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("court_domain", sa.String(64), sa.ForeignKey("court.domain"), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("content_kind", sa.String(16), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
    )

    op.create_table(
        "case",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("court_domain", sa.String(64), sa.ForeignKey("court.domain"), nullable=False),
        sa.Column("cartoteka_id", sa.String(16), sa.ForeignKey("cartoteka.id"), nullable=False),
        sa.Column("case_id", sa.String(32), nullable=True),
        sa.Column("case_uid", sa.String(64), nullable=True),
        sa.Column("case_number", sa.Text(), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=True),
        sa.Column("essence", sa.Text(), nullable=True),
        sa.Column("judge", sa.Text(), nullable=True),
        sa.Column("decision_date", sa.Date(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("legal_force_date", sa.Date(), nullable=True),
        sa.Column("card_url", sa.Text(), nullable=True),
        sa.Column("act_published", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("court_domain", "case_uid", name="uq_case_court_uid"),
    )
    op.create_index("ix_case_receipt_date", "case", ["receipt_date"])
    op.create_index("ix_case_decision_date", "case", ["decision_date"])
    op.execute('CREATE INDEX ix_case_number_trgm ON "case" USING gin (case_number gin_trgm_ops)')

    op.create_table(
        "act",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "case_pk",
            sa.BigInteger(),
            sa.ForeignKey("case.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("doc_number", sa.String(32), nullable=False),
        sa.Column("text_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("publ_date", sa.Date(), nullable=True),
        sa.UniqueConstraint("case_pk", "doc_number", "text_number", name="uq_act_case_doc"),
    )
    op.create_index("ix_act_publ_date", "act", ["publ_date"])

    op.create_table(
        "act_text",
        sa.Column(
            "act_id", sa.BigInteger(), sa.ForeignKey("act.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("raw_page_id", sa.BigInteger(), sa.ForeignKey("raw_page.id"), nullable=True),
        sa.Column("plain_text", sa.Text(), nullable=False),
    )
    # Полнотекст по русской конфигурации. Generated-колонка, а не триггер:
    # рассинхронизировать её нечем.
    op.execute(
        "ALTER TABLE act_text ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('russian', plain_text)) STORED"
    )
    op.execute("CREATE INDEX ix_act_text_tsv ON act_text USING gin (tsv)")

    op.create_table(
        "harvest_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("court_domain", sa.String(64), sa.ForeignKey("court.domain"), nullable=False),
        sa.Column("cartoteka_id", sa.String(16), sa.ForeignKey("cartoteka.id"), nullable=False),
        sa.Column("axis", sa.String(16), nullable=False),
        sa.Column("window_from", sa.Date(), nullable=False),
        sa.Column("window_to", sa.Date(), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=True),
        sa.Column("fetched_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_harvest_run_window", "harvest_run", ["court_domain", "window_from"])


def downgrade() -> None:
    op.drop_table("harvest_run")
    op.drop_table("act_text")
    op.drop_table("act")
    op.drop_table("case")
    op.drop_table("raw_page")
    op.drop_table("cartoteka")
    op.drop_table("court")
    # Расширения не снимаем: они могут использоваться другими схемами в той же базе.
