"""act_published: три состояния вместо двух

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19

19.08.2026 портал перестал отдавать ссылки на тексты актов в последней
колонке перечня — при том, что 06.08.2026 они там были у каждой строки.
Тексты никуда не делись: карточка дела по-прежнему несёт их в `cont_doc1`.

Но вместе со ссылкой пропал дешёвый ПРИЗНАК публикации. Раньше пустая
последняя колонка означала «акт не опубликован» (262-ФЗ). Теперь она
не означает ничего.

`false` в этой колонке стало бы ложью: база утверждала бы, что акта нет,
хотя мы просто не смотрели. Поэтому колонка становится трёхзначной:

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
    # признак брался из колонки перечня, которой больше нет.
    op.execute('UPDATE "case" SET act_published = NULL WHERE act_published = false')


def downgrade() -> None:
    op.execute('UPDATE "case" SET act_published = false WHERE act_published IS NULL')
    op.alter_column(
        "case",
        "act_published",
        nullable=False,
        server_default=sa.text("false"),
    )
