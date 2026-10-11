"""Ссылки на нормы, вынутые из текстов актов (roadmap 6.2).

Одна строка на документ, нормы — массивом ключей вида `ГК:333` под GIN.
Не «строка на ссылку», как было записано в архитектурном решении: у акта
в среднем полтора десятка норм, и это 25 млн строк с индексом против
1,75 млн с массивом — при том же запросе «акты, где есть ГК:333».
И не колонкой в `act_text`: любое обновление строки там пересчитывает
`tsvector` и переписывает восемь гигабайт текста.

`source` — откуда документ: `act` (кассация и апелляция), `vsrf`, `ksrf`;
`doc_id` — ключ его таблицы текстов. `parser_version` — правила разбора:
изменятся — видно, что пересчитать (`harvester norms`).
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE norm_ref (
            source text NOT NULL,
            doc_id bigint NOT NULL,
            norms text[] NOT NULL,
            parser_version smallint NOT NULL,
            PRIMARY KEY (source, doc_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_norm_ref_norms ON norm_ref USING gin (norms)")


def downgrade() -> None:
    op.execute("DROP TABLE norm_ref")
