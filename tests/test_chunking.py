"""Нарезка акта на куски.

Требование к ней ровно одно и оно жёсткое: **ничего не теряется**. Кусок
не той длины ухудшит поиск, а выброшенный абзац сделает практику
ненаходимой — и молча, потому что в выдаче он выглядел бы просто
отсутствующим.
"""

from __future__ import annotations

from harvester.chunking import MAX_CHARS, chunk_act

ACT = "\n".join(
    [
        "ПЯТЫЙ КАССАЦИОННЫЙ СУД ОБЩЕЙ ЮРИСДИКЦИИ",
        "ОПРЕДЕЛЕНИЕ по делу № 88-2951/2026",
        "Судебная коллегия по гражданским делам рассмотрела кассационную жалобу " * 12,
        "УСТАНОВИЛА",
        "Истец обратился в суд с иском о признании недействительным договора. " * 20,
        "Суд первой инстанции в удовлетворении требований отказал. " * 18,
        "руководствуясь статьями 379.7, 390 ГПК РФ, судебная коллегия ОПРЕДЕЛИЛА",
        "Кассационную жалобу оставить без удовлетворения.",
    ]
)


def _words(text: str) -> set[str]:
    return set(text.split())


def test_nothing_is_lost(chunks_of=chunk_act) -> None:
    """Слова из акта обязаны найтись в кусках — все до одного."""
    chunks = chunks_of(ACT)
    assert _words(ACT) <= _words(" ".join(c.text for c in chunks))


def test_short_tail_is_glued_not_dropped() -> None:
    """Короткий хвост приклеивается к предыдущему куску.

    Выбросить его как «слишком мелкий» значит потерять резолютивную часть:
    она короткая, а именно её и ищут.
    """
    tail = "Кассационную жалобу оставить без удовлетворения."
    text = "\n".join(["Мотивировочная часть, длинная и подробная. " * 40, tail])

    chunks = chunk_act(text)
    assert tail in " ".join(c.text for c in chunks)


def test_chunks_stay_within_the_cap() -> None:
    """Перекрытие полезно, но не ценой куска, который модель обрежет."""
    for chunk in chunk_act(ACT):
        assert len(chunk.text) <= MAX_CHARS, f"кусок {chunk.ordinal} длиной {len(chunk.text)}"


def test_neighbours_overlap() -> None:
    """Мысль, разорванная швом, должна найтись хотя бы одним куском."""
    chunks = chunk_act(ACT)
    assert len(chunks) > 1, "фикстура должна резаться больше чем на один кусок"

    overlaps = [
        chunks[i].last_paragraph >= chunks[i + 1].first_paragraph for i in range(len(chunks) - 1)
    ]
    assert any(overlaps), "ни один шов не перекрыт"


def test_long_paragraph_splits_on_word_boundary() -> None:
    """Разрубленное пополам слово не найдётся ни в одном куске."""
    text = "Довод заявителя о несоразмерности неустойки последствиям нарушения. " * 60

    chunks = chunk_act(text)
    assert _words(text) <= _words(" ".join(c.text for c in chunks))
    assert all(len(c.text) <= MAX_CHARS for c in chunks)


def test_legacy_text_without_paragraphs_still_chunks() -> None:
    """Собранные до 20.08 тексты лежат одной строкой — нарезка обязана
    справляться и с ними, иначе они выпадут из поиска до переразбора."""
    flat = ACT.replace("\n", " ")

    chunks = chunk_act(flat)
    assert chunks
    assert _words(flat) <= _words(" ".join(c.text for c in chunks))


def test_empty_text_gives_no_chunks() -> None:
    """Кусок-пустышка позже выглядел бы как найденный ответ."""
    assert chunk_act("") == []
    assert chunk_act("   \n  \n ") == []
