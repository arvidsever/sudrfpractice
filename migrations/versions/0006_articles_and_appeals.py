"""Статьи, результат в отношении лица и таблица жалоб

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-19

Карточка устроена по-разному в зависимости от вида производства, и первый
разбор это упустил. Проверено живьём на 5 КСОЮ:

    гражданские и КАС — «УЧАСТНИКИ»
    уголовные        — «ЛИЦА» (с перечнем статей) и «СТОРОНЫ»
    КоАП             — «СТОРОНЫ ПО ДЕЛУ» (с перечнем статей)

Парсер, знающий только «УЧАСТНИКОВ», по уголовным и КоАП вернул бы пустой
список — и счётчики бы при этом сошлись.

Заодно выяснилось, что перечень вменяемых статей есть прямо в карточке.
Это снимает нужду ходить за нормами во внешний источник и закрывает
половину задачи 6.2 плана.

Таблица «ЖАЛОБЫ» — половина сюжета кассации: жалобу сперва изучает судья,
и до заседания доходит не всякая.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("participant", sa.Column("articles", sa.Text(), nullable=True))
    op.add_column("participant", sa.Column("outcome", sa.Text(), nullable=True))
    op.execute(
        "CREATE INDEX ix_participant_articles_trgm ON participant USING gin (articles gin_trgm_ops)"
    )

    op.create_table(
        "appeal",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "case_pk", sa.BigInteger(), sa.ForeignKey("case.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("filed_at", sa.Date(), nullable=True),
        sa.Column("applicant_status", sa.Text(), nullable=True),
        sa.Column("applicant", sa.Text(), nullable=True),
        sa.Column("passed_to_study_at", sa.Date(), nullable=True),
        sa.Column("with_case_request", sa.Text(), nullable=True),
        sa.Column("ruling_date", sa.Date(), nullable=True),
        sa.Column("study_result", sa.Text(), nullable=True),
    )
    op.create_index("ix_appeal_case", "appeal", ["case_pk"])


def downgrade() -> None:
    op.drop_table("appeal")
    op.execute("DROP INDEX IF EXISTS ix_participant_articles_trgm")
    op.drop_column("participant", "outcome")
    op.drop_column("participant", "articles")
