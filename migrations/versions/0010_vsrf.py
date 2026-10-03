"""ВС РФ: тексты актов и производства-«дела» (docs/vsrf.md).

Своими таблицами: производство ВС не ложится в `case` с его картотекой
и `case_id`. Связь с нашим корпусом — по УИД, и для неё индекс
на `vsrf_claim.case_uid`. Полнотекст — такой же, как у `act_text`:
generated-колонка `tsvector` по русской конфигурации и GIN поверх, чтобы
тексты ВС искались тем же запросом, что и тексты кассации.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Сырьё ссылается на суд-источник, и ВС — тоже суд. Строкой здесь,
    # а не в справочниках из Sudrf: те — про суды на платформе ГАС.
    op.execute(
        "INSERT INTO court (domain, number, title, level, regions, has_captcha) "
        "VALUES ('www.vsrf.ru', NULL, 'Верховный Суд Российской Федерации', "
        "'supreme', '[]', false) ON CONFLICT (domain) DO NOTHING"
    )
    op.create_table(
        "vsrf_act",
        sa.Column("pdf_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("number", sa.Text(), nullable=True),
        sa.Column("claim_id", sa.String(32), nullable=True),
        sa.Column("act_kind", sa.Text(), nullable=True),
        sa.Column("act_date", sa.Date(), nullable=True),
        sa.Column("case_type", sa.String(32), nullable=False),
        sa.Column("instance", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("collegium", sa.Text(), nullable=True),
        sa.Column("judge", sa.Text(), nullable=True),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("text_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_vsrf_act_claim", "vsrf_act", ["claim_id"])
    # Очередь текстов: «ещё не брали», от свежего к старому.
    op.create_index(
        "ix_vsrf_act_pending",
        "vsrf_act",
        ["act_date"],
        postgresql_where=sa.text("text_fetched_at IS NULL"),
    )

    op.create_table(
        "vsrf_act_text",
        sa.Column(
            "pdf_id",
            sa.BigInteger(),
            sa.ForeignKey("vsrf_act.pdf_id", ondelete="CASCADE"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column("raw_page_id", sa.BigInteger(), sa.ForeignKey("raw_page.id"), nullable=True),
        sa.Column("plain_text", sa.Text(), nullable=False),
    )
    op.execute(
        "ALTER TABLE vsrf_act_text ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('russian', plain_text)) STORED"
    )
    op.execute("CREATE INDEX ix_vsrf_act_text_tsv ON vsrf_act_text USING gin (tsv)")

    op.create_table(
        "vsrf_claim",
        sa.Column("claim_id", sa.String(32), primary_key=True),
        sa.Column("number", sa.Text(), nullable=True),
        sa.Column("received_date", sa.Date(), nullable=True),
        sa.Column("case_type", sa.String(32), nullable=False),
        sa.Column("instance", sa.Text(), nullable=True),
        sa.Column("case_uid", sa.Text(), nullable=True),
        sa.Column("first_court", sa.Text(), nullable=True),
        sa.Column("first_case_number", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_vsrf_claim_uid", "vsrf_claim", ["case_uid"])

    op.create_table(
        "vsrf_window",
        sa.Column("source", sa.String(16), primary_key=True),
        sa.Column("case_type", sa.String(32), primary_key=True),
        sa.Column("window_from", sa.Date(), primary_key=True),
        sa.Column("window_to", sa.Date(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column(
            "done_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("vsrf_window")
    op.drop_table("vsrf_claim")
    op.drop_table("vsrf_act_text")
    op.drop_table("vsrf_act")
    op.execute("DELETE FROM court WHERE domain = 'www.vsrf.ru'")
