"""Версия извлечения текста у решений КС.

Режим pypdf по умолчанию рвёт слова в PDF Конституционного Суда: «суде й»,
«жало ба» — 12,7 разрыва на тысячу слов против 1,0 в режиме `layout`
(замер 11.10.2026 на 30 решениях разных лет). Тексты переизвлекаются
из PDF в S3; версия нужна, чтобы проход был возобновляемым и чтобы
было видно, каким способом получен текст. 1 — прежний режим, 2 — `layout`.
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ksrf_decision_text ADD COLUMN text_version smallint NOT NULL DEFAULT 1")


def downgrade() -> None:
    op.execute("ALTER TABLE ksrf_decision_text DROP COLUMN text_version")
