"""Замер объёма картотек и очередь заданий обхода

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-19

Две таблицы, обе про одно: сделать объём работы наблюдаемым.

`cartoteka_volume` — сколько всего дел в каждой паре суд × картотека.
Один запрос без фильтра дат даёт счётчик, и оценка корпуса перестаёт быть
оценкой. Без этого числа нельзя ни спланировать обход, ни понять, где он
недобрал.

`harvest_task` — очередь окон. Пока обход запускался руками, «сколько
осталось» было вопросом к памяти. Очередь превращает его в запрос.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cartoteka_volume",
        sa.Column("court_domain", sa.String(64), sa.ForeignKey("court.domain"), primary_key=True),
        sa.Column("cartoteka_id", sa.String(16), sa.ForeignKey("cartoteka.id"), primary_key=True),
        sa.Column("total_cases", sa.Integer(), nullable=True, comment="счётчик без фильтра дат"),
        sa.Column(
            "status", sa.String(16), nullable=False, comment="measured | empty | throttled | failed"
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "measured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "harvest_task",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("court_domain", sa.String(64), sa.ForeignKey("court.domain"), nullable=False),
        sa.Column("cartoteka_id", sa.String(16), sa.ForeignKey("cartoteka.id"), nullable=False),
        sa.Column("axis", sa.String(16), nullable=False),
        sa.Column("window_from", sa.Date(), nullable=False),
        sa.Column("window_to", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment="pending | running | done | empty | failed | throttled",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cases_found", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("harvest_run.id"), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "court_domain",
            "cartoteka_id",
            "axis",
            "window_from",
            "window_to",
            name="uq_task_window",
        ),
    )
    # Очередь читается ровно одним способом: «дай следующее незакрытое».
    op.create_index("ix_task_pending", "harvest_task", ["status", "court_domain", "window_from"])


def downgrade() -> None:
    op.drop_table("harvest_task")
    op.drop_table("cartoteka_volume")
