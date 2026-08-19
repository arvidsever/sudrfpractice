"""Разбор дат портала.

Портал отдаёт даты как `дд.мм.гггг` и охотно отдаёт пустую ячейку: у дела
может не быть даты решения или даты вступления в силу. Пустое значение —
это `None`, а не ошибка и не сегодняшнее число.
"""

from __future__ import annotations

from datetime import date, datetime

PORTAL_FORMAT = "%d.%m.%Y"


def parse(value: str | None) -> date | None:
    """Разобрать дату из строки выдачи. Мусор в ячейке даёт `None`, а не исключение.

    Разбор здесь мягкий намеренно: строка выдачи несёт реквизиты десятков тысяч
    дел, и одна нестандартная ячейка не должна ронять обход целого суда.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, PORTAL_FORMAT).date()
    except ValueError:
        return None
