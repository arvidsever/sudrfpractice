"""Ссылки на нормы в текстах актов (roadmap 6.2).

«Практика по ст. 333 ГК» — это запрос, а не полнотекстовый поиск: слова
«статья 333» в тексте пишут десятком способов, а число 333 встречается
и в суммах. Здесь ссылка приводится к ключу `ГК:333`, и поиск идёт по нему.

Что берём и чего не берём — намеренно узко, точность важнее охвата:

* только кодексы и Конституцию. Законы по номеру («№ 2300-1») — следующий
  шаг, когда появится запрос на них;
* только «статья N <кодекс>», где кодекс назван сразу за номером. «Статья 333
  названного Кодекса» не разбирается: чтобы понять, какого, нужен контекст,
  а ошибка здесь хуже пропуска;
* только номер статьи. Пункты и части отбрасываются: «п. 1 ст. 333 ГК»
  и «статья 333 ГК» — одна и та же норма для поиска практики;
* диапазон «статьи 131–132» даёт оба конца, но не середину.

Версия разбора пишется рядом с результатом: правила изменятся, и тогда
видно, какие строки пересчитать.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import Engine, text

log = logging.getLogger("harvester.norms")

PARSER_VERSION = 1

#: Кодекс → как его называют в текстах. Длинные названия раньше коротких:
#: «Гражданский процессуальный кодекс» не должен совпасть как «Гражданский кодекс».
_CODES = [
    ("ГПК", r"Гражданск\w+\s+процессуальн\w+\s+кодекс\w*|ГПК"),
    ("АПК", r"Арбитражн\w+\s+процессуальн\w+\s+кодекс\w*|АПК"),
    ("УПК", r"Уголовно-\s?процессуальн\w+\s+кодекс\w*|УПК"),
    ("УИК", r"Уголовно-\s?исполнительн\w+\s+кодекс\w*|УИК"),
    ("КАС", r"Кодекс\w*\s+административного\s+судопроизводства|КАС"),
    (
        "КоАП",
        r"Кодекс\w*\s+(?:Российской\s+Федерации\s+|РФ\s+)?об\s+административных"
        r"\s+правонарушениях|КоАП",
    ),
    ("ГрК", r"Градостроительн\w+\s+кодекс\w*|ГрК"),
    ("ГК", r"Гражданск\w+\s+кодекс\w*|ГК"),
    ("УК", r"Уголовн\w+\s+кодекс\w*|УК"),
    ("ТК", r"Трудов\w+\s+кодекс\w*|ТК"),
    ("ЖК", r"Жилищн\w+\s+кодекс\w*|ЖК"),
    ("СК", r"Семейн\w+\s+кодекс\w*|СК"),
    ("ЗК", r"Земельн\w+\s+кодекс\w*|ЗК"),
    ("НК", r"Налогов\w+\s+кодекс\w*|НК"),
    ("БК", r"Бюджетн\w+\s+кодекс\w*|БК"),
    ("Конституция", r"Конституци\w+"),
]

_NUMBER = r"\d+(?:\.\d+)*"
_LIST = rf"{_NUMBER}(?:\s*(?:,|и|или|-|–|—)\s*{_NUMBER})*"
_CODE = "|".join(f"(?P<c{i}>{pattern})" for i, (_, pattern) in enumerate(_CODES))

#: «статьи 330, 333 ГК РФ», «ст.ст. 15 и 1064 Гражданского кодекса», «ст.333 ГК».
_REFERENCE = re.compile(
    rf"(?:стать\w*|ст\.(?:\s?ст\.)?)\s*(?P<numbers>{_LIST})\s+(?:{_CODE})(?!\w)",
    re.IGNORECASE,
)
#: Запрос пользователя свободнее текста акта: «333 ГК», «ст 333 гк рф».
_QUERY = re.compile(rf"(?P<numbers>{_LIST})\s*(?:{_CODE})(?!\w)", re.IGNORECASE)


def _keys(match: re.Match) -> set[str]:
    code = next(name for i, (name, _) in enumerate(_CODES) if match.group(f"c{i}") is not None)
    return {f"{code}:{number}" for number in re.findall(_NUMBER, match.group("numbers"))}


def extract(plain_text: str) -> list[str]:
    """Нормы, на которые ссылается текст: отсортированные ключи `ГК:333`."""
    found: set[str] = set()
    for match in _REFERENCE.finditer(plain_text):
        found |= _keys(match)
    return sorted(found)


def parse_query(value: str) -> list[str]:
    """Норма из запроса: «ст. 333 ГК», «333 ГК РФ», «статья 12.8 КоАП».
    Пустой список — не разобрали, и молча искать «всё» в этом случае нельзя."""
    found: set[str] = set()
    for match in _QUERY.finditer(value):
        found |= _keys(match)
    return sorted(found)


#: Откуда читать тексты: источник → таблица и её ключ.
SOURCES = {
    "act": ("act_text", "act_id"),
    "vsrf": ("vsrf_act_text", "pdf_id"),
    "ksrf": ("ksrf_decision_text", "pdf_id"),
}


def backfill(engine: Engine, *, batch: int = 2000, limit: int | None = None) -> dict[str, int]:
    """Разобрать тексты, у которых норм ещё нет или они разобраны старой
    версией правил. Идёт по ключу, поэтому прерванный проход продолжается сам."""
    result = dict.fromkeys(SOURCES, 0)
    for source, (table, key) in SOURCES.items():
        pending = text(
            f"""
            SELECT t.{key} AS doc_id, t.plain_text
            FROM {table} t
            LEFT JOIN norm_ref n ON n.source = :source AND n.doc_id = t.{key}
            WHERE t.{key} > :after AND (n.doc_id IS NULL OR n.parser_version < :version)
            ORDER BY t.{key}
            LIMIT :batch
            """  # noqa: S608 — имена таблиц из словаря выше, не из ввода
        )
        after = -1
        while limit is None or result[source] < limit:
            with engine.connect() as connection:
                rows = connection.execute(
                    pending,
                    {"source": source, "after": after, "version": PARSER_VERSION, "batch": batch},
                ).all()
            if not rows:
                break
            after = rows[-1].doc_id
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO norm_ref (source, doc_id, norms, parser_version)
                        VALUES (:source, :doc_id, CAST(:norms AS text[]), :version)
                        ON CONFLICT (source, doc_id) DO UPDATE
                        SET norms = excluded.norms, parser_version = excluded.parser_version
                        """
                    ),
                    [
                        {
                            "source": source,
                            "doc_id": row.doc_id,
                            "norms": extract(row.plain_text),
                            "version": PARSER_VERSION,
                        }
                        for row in rows
                    ],
                )
            result[source] += len(rows)
            log.info("нормы: %s", result)
    return result
