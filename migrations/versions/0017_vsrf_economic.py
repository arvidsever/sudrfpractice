"""Экономическая коллегия ВС (СКЭС): акты в той же таблице, с пометкой.

До 18.10.2026 строки СКЭС считались при сверке со счётчиком и не сохранялись
(решение владельца 03.10.2026). Решение пересмотрено: это около 150 тысяч
актов, и они уже проходят через выдачу «административных» и гражданских дел.
Номера PDF у СКЭС из той же нумерации, что у остальных, и не пересекаются
с ними — отдельная таблица не нужна.

`economic` — акт СКЭС: PDF по адресу `/stor_pdf_ec/`, вместо карточки ВС —
ссылка в картотеку арбитражных судов. `arbitr_number` — номер арбитражного
дела из этой ссылки (`А40-128545/2025`): ключ связи с КАД, когда до неё дойдёт.
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE vsrf_act ADD COLUMN economic boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE vsrf_act ADD COLUMN arbitr_number text")
    op.execute(
        "CREATE INDEX ix_vsrf_act_arbitr_number ON vsrf_act (arbitr_number) "
        "WHERE arbitr_number IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE vsrf_act DROP COLUMN arbitr_number")
    op.execute("ALTER TABLE vsrf_act DROP COLUMN economic")
