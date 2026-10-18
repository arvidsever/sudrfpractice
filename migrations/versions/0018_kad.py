"""КАД: дела СИП и арбитражных судов округов, их акты и тексты (docs/kad.md).

Своими таблицами: у арбитражного дела нет ни картотеки ГАС, ни УИД,
а связь с СКЭС — по номеру дела (`vsrf_act.arbitr_number`).

* `kad_case` — дело из выдачи поиска: идентификатор КАД, номер, суд,
  по которому нашлось, дата регистрации. Стороны не храним: в выдаче
  у граждан ФИО с ИНН.
* `kad_doc` — опубликованный акт инстанции нашего суда со своей
  классификацией КАД. `wanted` — качать ли текст: итоговые акты
  и короткий список полезных определений (`harvester/kad.py`).
* `kad_doc_text` — текст из PDF, полнотекст как у остальных корпусов.
* `kad_window` — окно поиска, сверенное со счётчиком.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO court (domain, number, title, level, regions, has_captcha) "
        "VALUES ('kad.arbitr.ru', NULL, 'Картотека арбитражных дел', "
        "'arbitr', '[]', false) ON CONFLICT (domain) DO NOTHING"
    )
    op.create_table(
        "kad_case",
        sa.Column("case_id", sa.Text(), primary_key=True),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("court_tag", sa.Text(), nullable=False),
        sa.Column("case_kind", sa.Text(), nullable=True),
        sa.Column("registration_date", sa.Date(), nullable=True),
        sa.Column("judge", sa.Text(), nullable=True),
        sa.Column("docs_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_kad_case_number", "kad_case", ["number"])
    op.create_table(
        "kad_doc",
        sa.Column("doc_id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("kad_case.case_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("court_tag", sa.Text(), nullable=False),
        sa.Column("instance_level", sa.Integer(), nullable=True),
        sa.Column("type_name", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("finish_instance", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("publish_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("wanted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("text_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_kad_doc_case", "kad_doc", ["case_id"])
    op.create_index(
        "ix_kad_doc_pending",
        "kad_doc",
        ["publish_date"],
        postgresql_where=sa.text("wanted AND text_fetched_at IS NULL"),
    )
    op.create_table(
        "kad_doc_text",
        sa.Column(
            "doc_id",
            sa.Text(),
            sa.ForeignKey("kad_doc.doc_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("raw_page_id", sa.BigInteger(), sa.ForeignKey("raw_page.id"), nullable=True),
        sa.Column("plain_text", sa.Text(), nullable=False),
    )
    op.execute(
        "ALTER TABLE kad_doc_text ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('russian', plain_text)) STORED"
    )
    op.execute("CREATE INDEX ix_kad_doc_text_tsv ON kad_doc_text USING gin (tsv)")
    op.create_table(
        "kad_window",
        sa.Column("court_tag", sa.Text(), primary_key=True),
        sa.Column("window_from", sa.Date(), primary_key=True),
        sa.Column("window_to", sa.Date(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column(
            "done_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("kad_window")
    op.drop_table("kad_doc_text")
    op.drop_table("kad_doc")
    op.drop_table("kad_case")
    op.execute("DELETE FROM court WHERE domain = 'kad.arbitr.ru'")
