"""act_published: три состояния вместо двух

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19

Пустая колонка «Судебные акты» в перечне не означает «акт не опубликован».
Она бывает пустой ещё и потому, что перечень запрошен с коротким `delo_id`
(см. `docs/delo-id-and-act-links.md`).

`false` в такой ситуации было бы ложью: база утверждала бы отсутствие акта,
хотя карточку никто не открывал, а текст опубликован. Поэтому колонка
становится трёхзначной:

* `true`  — текст акта у нас есть либо ссылка на него была;
* `false` — карточку открыли, текста в ней нет (262-ФЗ);
* `null`  — не проверяли.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("case", "act_published", nullable=True, server_default=None)
    # Всё, что записано до этой миграции как false, на деле «не знаем»:
    # признак брался из колонки перечня, которая могла быть скрыта.
    op.execute('UPDATE "case" SET act_published = NULL WHERE act_published = false')


def downgrade() -> None:
    op.execute('UPDATE "case" SET act_published = false WHERE act_published IS NULL')
    op.alter_column(
        "case",
        "act_published",
        nullable=False,
        server_default=sa.text("false"),
    )
