"""Судебный УИД дела — ключ связи между инстанциями.

`case.case_uid` — не он: это внутренний идентификатор записи на сайте суда
(GUID из адреса карточки), у каждой инстанции свой. Судебный УИД вида
`77RS0016-02-2023-023173-37` присваивается делу в первой инстанции и идёт
с ним через апелляцию, кассацию и Верховный Суд. Карточка его показывает,
разбор его читал — но в базу он не попадал, и 11.10.2026 связь ВС
с кассацией по `case_uid` дала ноль совпадений из 41 719.

Пустая строка — «карточку разобрали, УИД в ней нет»; NULL — «не смотрели».
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('ALTER TABLE "case" ADD COLUMN judicial_uid text')
    op.execute(
        'CREATE INDEX ix_case_judicial_uid ON "case" (judicial_uid) '
        "WHERE judicial_uid IS NOT NULL AND judicial_uid <> ''"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_case_judicial_uid")
    op.execute('ALTER TABLE "case" DROP COLUMN judicial_uid')
