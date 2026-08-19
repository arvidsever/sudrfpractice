"""Кодировка windows-1251 — сквозное требование платформы ГАС.

Портал отдаёт страницы в cp1251 и в ней же ждёт значения параметров запроса.
UTF-8 в запросе не ошибка формата, а тихий промах: сервер отвечает 200
и пустой выдачей, поэтому кодирование вынесено в отдельный модуль и
проходит через один-единственный путь.
"""

from __future__ import annotations

from urllib.parse import quote

CP1251 = "cp1251"

#: Значение кнопки «Найти» в cp1251. Без него выдачи нет вовсе —
#: сервер отдаёт форму поиска (§1 грамматики).
SUBMIT_VALUE = "%CD%E0%E9%F2%E8"


def percent_encode(value: str) -> str:
    """Percent-encoding значения параметра через cp1251, а не UTF-8."""
    return quote(value.encode(CP1251), safe="")


def decode(content: bytes) -> str:
    """Разобрать ответ суда. `replace` — чтобы одна битая буква не роняла разбор."""
    return content.decode(CP1251, errors="replace")
