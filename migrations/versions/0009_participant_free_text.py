"""Реквизиты участника — свободный текст, а не поле фиксированной длины.

04.09.2026 свод карточек трёх суток стоял из-за одной строки:
`kpp='СНТ "Торгреклама 89"'`, двадцать знаков в колонке `varchar(16)`.
В карточке 4 КСОЮ под заголовком «КПП» лежало название организации —
так заполнил суд, и портал отдал как есть.

Дальше цепочка: вставка участников падает, транзакция откатывается,
исключение выходит из потока суда и убивает его до конца прогона.
4, 1, 2 и 7 КСОЮ так простояли трое суток, а это 126 тысяч карточек
из 165 оставшихся.

Ограничение длины не купило ничего: проверять ИНН на десять цифр оно
всё равно не умело, а сломать сбор — сломало. Поле хранит то, что сказал
портал; разбираться, ИНН это или название СНТ, — дело слоя выше.

`hearing_time` того же происхождения: восемь знаков под «10:30» ровно
до первой карточки, где суд напишет «10:30–11:00».
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

COLUMNS = (
    ("participant", "inn"),
    ("participant", "kpp"),
    ("participant", "ogrn"),
    ("participant", "ogrnip"),
    ("hearing", "hearing_time"),
)


def upgrade() -> None:
    for table, column in COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text")


def downgrade() -> None:
    # Обратно — с усечением: иначе откат упадёт на тех же строках,
    # из-за которых миграция и понадобилась.
    for table, column, length in (
        ("participant", "inn", 16),
        ("participant", "kpp", 16),
        ("participant", "ogrn", 20),
        ("participant", "ogrnip", 20),
        ("hearing", "hearing_time", 8),
    ):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE varchar({length}) USING left({column}, {length})"
        )
