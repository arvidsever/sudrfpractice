"""Нарезка текста акта на куски под эмбеддинги.

Модель читает за раз ограниченный кусок, а кассационное определение —
13 тысяч знаков в среднем и 41 тысяча в пределе. Значит его надо резать,
и вопрос только в том, по какому шву.

Шов выбран по замеру, а не по наитию. У 306 собранных актов:

    абзацев на акт      медиана 49, от 22 до 135
    длина абзаца        медиана 208 знаков, 90-й процентиль 567
    длиннее 2000 знаков 9 абзацев из 15 892 — одна десятая процента

То есть абзац почти всегда МЕНЬШЕ куска, и нарезка — это сборка соседних
абзацев, а не дробление. Разбирать предложения не нужно вовсе, и хорошо:
в юридическом тексте наивный разделитель по точке рубит «ст. 333 ГК РФ»,
«п. 1 ч. 2» и «г. Пятигорск».

Два решения, которые стоит помнить:

* **куски меряются знаками, а не токенами.** Токенизатор свой у каждой
  модели, и привязка к нему означала бы пересчёт всей нарезки при смене
  модели. Для русского один токен — примерно три знака, и запас в границе
  дешевле такой связанности;
* **соседние куски перекрываются одним абзацем.** Мысль, разорванная
  швом, иначе не находится ни одним из кусков: в первом нет вывода,
  во втором — оснований.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Целевой размер куска в знаках. ~340 токенов для русского, с запасом
#: под окно любой из рассматриваемых моделей, включая 512-токенные.
TARGET_CHARS = 1000

#: Потолок: абзац длиннее приходится делить принудительно.
MAX_CHARS = 2000

#: Куски короче этого не отдаём отдельно — обрывок в одну строку
#: («Судья I инстанции — ФИО2») не несёт смысла, который стоит искать.
MIN_CHARS = 80


@dataclass(frozen=True, slots=True)
class Chunk:
    """Кусок текста и его место в акте."""

    ordinal: int
    text: str
    #: Номера абзацев, вошедших в кусок, — по ним кусок находится в акте.
    first_paragraph: int
    last_paragraph: int


def _paragraphs(text: str) -> list[str]:
    """Абзацы текста. Для старых записей без переводов строки — весь текст."""
    lines = [line.strip() for line in text.split("\n")]
    return [line for line in lines if line]


def _split_long(paragraph: str, limit: int = MAX_CHARS) -> list[str]:
    """Разделить слишком длинный абзац.

    Режем по границе предложения, но осторожно: точка в юридическом тексте
    чаще сокращение, чем конец фразы. Поэтому границей считается точка,
    за которой пробел и ЗАГЛАВНАЯ буква, — «ст. 333» так не разорвётся,
    а «…отказано. Судебная коллегия…» разорвётся.

    Если и это не помогло (сплошной текст без границ), режем по длине:
    потерять нельзя ничего, а кусок сверх меры модель просто обрежет.
    """
    if len(paragraph) <= limit:
        return [paragraph]

    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z])", paragraph):
        if current and len(current) + len(sentence) + 1 > limit:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)

    forced: list[str] = []
    for part in parts:
        while len(part) > limit:
            # По границе слова, а не по счётчику знаков: разрубленное
            # пополам слово не найдётся ни в одном куске и вдобавок
            # ломает сверку «ничего не потеряно».
            cut = part.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            forced.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            forced.append(part)
    return forced


def chunk_act(text: str) -> list[Chunk]:
    """Собрать абзацы в куски целевого размера.

    Возвращает пустой список для пустого текста: нечего резать — нечего
    и отдавать, а кусок-пустышка позже выглядел бы как найденный ответ.
    """
    paragraphs: list[tuple[int, str]] = []
    for number, paragraph in enumerate(_paragraphs(text)):
        for piece in _split_long(paragraph):
            paragraphs.append((number, piece))

    if not paragraphs:
        return []

    chunks: list[Chunk] = []
    current: list[tuple[int, str]] = []
    length = 0

    def flush() -> None:
        nonlocal current, length
        if not current:
            return
        body = " ".join(piece for _, piece in current)
        if len(body) < MIN_CHARS and chunks:
            # Обрывок не выбрасываем — приклеиваем к предыдущему куску.
            # Выбросить значило бы потерять текст молча, а это ровно
            # то, от чего проект бережётся везде остальном.
            previous = chunks[-1]
            chunks[-1] = Chunk(
                ordinal=previous.ordinal,
                text=f"{previous.text} {body}".strip(),
                first_paragraph=previous.first_paragraph,
                last_paragraph=current[-1][0],
            )
        else:
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    text=body,
                    first_paragraph=current[0][0],
                    last_paragraph=current[-1][0],
                )
            )
        # Перекрытие: последний абзац переходит в следующий кусок, чтобы
        # мысль, разорванная швом, нашлась хотя бы одним из них.
        current = current[-1:] if len(current) > 1 else []
        length = sum(len(piece) for _, piece in current)

    for number, piece in paragraphs:
        if current and length + len(piece) > TARGET_CHARS:
            flush()
            # Перекрытие полезно, но не любой ценой: если перенесённый
            # абзац сам крупный, вместе с новым он вылезет за потолок.
            # Тогда перекрытие уступает — размер куска важнее.
            if current and length + len(piece) > MAX_CHARS:
                current, length = [], 0
        current.append((number, piece))
        length += len(piece)
    flush()

    return chunks
