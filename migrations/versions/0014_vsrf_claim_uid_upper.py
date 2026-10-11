"""Индекс под связь ВС с корпусом по судебному УИД.

ВС пишет УИД как придётся — встречается `23os0000-…` строчными, — поэтому
сравнение идёт по `upper(case_uid)`, и индекс нужен по тому же выражению:
иначе отметка «дошло до ВС» перебирала бы 234 тысячи производств
на каждую строку выдачи.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX ix_vsrf_claim_uid_upper ON vsrf_claim (upper(case_uid))")


def downgrade() -> None:
    op.execute("DROP INDEX ix_vsrf_claim_uid_upper")
