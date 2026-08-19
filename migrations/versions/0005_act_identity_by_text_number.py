"""Акт опознаётся номером вкладки, а не номером документа

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-19

Тексты теперь берутся из карточки, а она номера документа не показывает —
он есть только в ссылке из перечня. При этом дела, найденные по оси
поступления, приходят вообще без ссылки на акт: акт ещё не опубликован
на момент, когда дело попало в окно.

Значит опознавать акт внутри дела надо тем, что есть всегда, — порядковым
номером вкладки. `doc_number` остаётся как метаданное портала: он полезен,
чтобы собрать прямую ссылку, но identity на нём строить нельзя.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("act", "doc_number", existing_type=sa.String(32), nullable=True)
    op.drop_constraint("uq_act_case_doc", "act", type_="unique")
    op.create_unique_constraint("uq_act_case_text", "act", ["case_pk", "text_number"])
    op.alter_column("act", "url", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM act WHERE doc_number IS NULL OR url IS NULL")
    op.alter_column("act", "url", existing_type=sa.Text(), nullable=False)
    op.drop_constraint("uq_act_case_text", "act", type_="unique")
    op.create_unique_constraint("uq_act_case_doc", "act", ["case_pk", "doc_number", "text_number"])
    op.alter_column("act", "doc_number", existing_type=sa.String(32), nullable=False)
