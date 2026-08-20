"""Индексы под фасеты поиска.

Выдача уже сама по себе даёт реквизиты, по которым юрист ищет практику:
суд, картотека, судья, результат, даты, суд первой инстанции. Без индексов
каждый такой запрос — полный проход по таблице, а она растёт к трём
миллионам строк.

Индексы строятся `CONCURRENTLY`: сбор идёт круглосуточно и пишет в `case`
непрерывно, а обычный `CREATE INDEX` блокирует запись на всё время
построения. Отсюда же `autocommit_block` — вне транзакции эта форма
не работает.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

#: Фасет: имя индекса и выражение. Судья и результат — списком значений,
#: поэтому btree; по ним же считаются счётчики фасетов.
#: `case` — зарезервированное слово SQL, поэтому имя таблицы в кавычках.
FACETS = [
    ("ix_case_court_cartoteka", '"case" (court_domain, cartoteka_id)'),
    ("ix_case_judge", '"case" (judge)'),
    ("ix_case_result", '"case" (result)'),
    ("ix_case_lower_court", '"case" (lower_court)'),
    # Частый запрос — «дела этого суда за такой-то период»: одна колонка
    # сужает, вторая упорядочивает.
    ("ix_case_court_decision_date", '"case" (court_domain, decision_date)'),
]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, target in FACETS:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {target}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ in FACETS:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
