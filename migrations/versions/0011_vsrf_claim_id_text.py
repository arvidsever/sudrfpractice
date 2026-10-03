"""Идентификатор карточки ВС — свободный текст.

У дисциплинарных дел он вида `1-10-3F834369BAAB45BFB3E06A8E1E034938`,
38 знаков, а колонка была на 32 — и сбор «дел» упал на первой же
странице. Тот же урок, что в 0009: ограничение длины ничего не купило,
а сломать сбор — сломало.
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE vsrf_claim ALTER COLUMN claim_id TYPE text")
    op.execute("ALTER TABLE vsrf_act ALTER COLUMN claim_id TYPE text")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE vsrf_act ALTER COLUMN claim_id TYPE varchar(32) USING left(claim_id, 32)"
    )
    op.execute(
        "ALTER TABLE vsrf_claim ALTER COLUMN claim_id TYPE varchar(32) USING left(claim_id, 32)"
    )
