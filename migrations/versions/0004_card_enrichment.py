"""Данные карточки дела: участники, слушания, нижестоящий суд

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-19

Свод текстов переезжает со страницы акта на карточку дела. Стоят они
одинаково — один запрос, — но карточка отдаёт сверх текста участников
с реквизитами, суд первой инстанции и движение по событиям. При нескольких
актах на дело она к тому же отдаёт все вкладки разом, тогда как
`name_op=doc` требует запроса на каждую.

Участники и слушания переписываются целиком при каждом обходе карточки:
карточка — источник истины, и частичное обновление тут только создало бы
призраков от прошлых разборов.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("appealed_act", sa.Text(), nullable=True),
        sa.Column("appeal_result", sa.Text(), nullable=True),
        sa.Column("lower_region", sa.Text(), nullable=True),
        sa.Column("lower_court", sa.Text(), nullable=True),
        sa.Column("lower_case_number", sa.Text(), nullable=True),
        sa.Column("lower_decision_date", sa.Date(), nullable=True),
        sa.Column("card_fetched_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("case", column)

    op.create_table(
        "participant",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "case_pk", sa.BigInteger(), sa.ForeignKey("case.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("inn", sa.String(16), nullable=True),
        sa.Column("kpp", sa.String(16), nullable=True),
        sa.Column("ogrn", sa.String(20), nullable=True),
        sa.Column("ogrnip", sa.String(20), nullable=True),
    )
    op.create_index("ix_participant_case", "participant", ["case_pk"])
    # Поиск практики по контрагенту — один из главных сценариев.
    op.execute("CREATE INDEX ix_participant_name_trgm ON participant USING gin (name gin_trgm_ops)")
    op.create_index("ix_participant_inn", "participant", ["inn"])

    op.create_table(
        "hearing",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "case_pk", sa.BigInteger(), sa.ForeignKey("case.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("hearing_date", sa.Date(), nullable=True),
        sa.Column("hearing_time", sa.String(8), nullable=True),
        sa.Column("place", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("published_at", sa.Date(), nullable=True),
    )
    op.create_index("ix_hearing_case", "hearing", ["case_pk"])


def downgrade() -> None:
    op.drop_table("hearing")
    op.drop_table("participant")
    for name in (
        "card_fetched_at",
        "lower_decision_date",
        "lower_case_number",
        "lower_court",
        "lower_region",
        "appeal_result",
        "appealed_act",
        "category",
    ):
        op.drop_column("case", name)
