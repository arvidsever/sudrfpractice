"""Разбор страницы текста судебного акта (`name_op=doc`) — §4 грамматики.

Текст лежит в **`div#content`**. `div#doccont` — это только кнопка печати,
он пуст, и ходить туда за текстом нельзя.

Внутри `div#content` сам акт приклеен как вложенный документ
(`<HTML><BODY><SPAN style="TEXT-ALIGN: justify">…`), а вокруг него — шапка
страницы и скрипт печати. Берём вложенный span: тогда текст совпадает
с блоком `cont_doc1` вкладки «Судебные акты» карточки символ в символ,
и одного запроса на дело действительно хватает.

По 262-ФЗ публикуемый текст обезличен: ФИО заменены на `ФИО1`/`ФИО2`,
даты внутри текста — на `ДД.ММ.ГГГГ`. Реквизиты берутся из строки выдачи,
из текста их не восстановить.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser, Node

from ..models import ActText

#: Признак конца перебора `text_number`, а не ошибка.
EMPTY_DOCUMENT_MARKER = "ПУСТОЙ ДОКУМЕНТ"


#: Насколько полно абзацы должны покрывать текст узла, чтобы им верить.
#: Если разметка вдруг окажется другой и часть текста будет лежать мимо
#: `<p>`, разбор по абзацам молча потеряет её — а терять нельзя.
_PARAGRAPH_COVERAGE = 0.95


def _plain_text(node: Node) -> str:
    """Текст узла: пробелы схлопнуты, абзацы сохранены переводом строки.

    Абзацы здесь не для красоты. Акт — это 13 тысяч знаков без единого
    переноса, если их выбросить, и дальше его нечем резать на куски под
    эмбеддинги: предложения в юридическом тексте наивным разделителем
    не берутся («ст. 333 ГК РФ», «п. 1 ч. 2», «г. Пятигорск»). Разметка
    же даёт готовые границы — у типового определения их около полусотни.

    Если абзацы покрывают не весь текст узла, берём узел целиком: лучше
    потерять структуру, чем часть акта.
    """
    for junk in node.css("script, style"):
        junk.decompose()

    whole = " ".join(node.text(separator=" ").split())

    paragraphs = [
        line for line in (" ".join(p.text(separator=" ").split()) for p in node.css("p")) if line
    ]
    if not paragraphs:
        return whole

    joined = "\n".join(paragraphs)
    covered = len(joined.replace("\n", " "))
    if covered < len(whole) * _PARAGRAPH_COVERAGE:
        return whole
    return joined


def parse_act(html: str, *, number: str, text_number: int = 1) -> ActText:
    """Достать текст акта. Пустой результат — повод для новой фикстуры, не для записи в базу."""
    content = HTMLParser(html).css_first("div#content")
    if content is None:
        raise ValueError("на странице нет div#content — вёрстка изменилась, нужна фикстура")

    # Вложенный документ: точное совпадение с блоком карточки.
    body = content.css_first('span[style*="justify"]')
    text = _plain_text(body) if body is not None else _plain_text(content)

    return ActText(number=number, text_number=text_number, text=text)


def parse_card_act(html: str, text_number: int = 1) -> str:
    """Текст акта из карточки дела (`cont_doc{N}`) — эталон для сверки с `name_op=doc`."""
    node = HTMLParser(html).css_first(f"#cont_doc{text_number}")
    if node is None:
        raise ValueError(f"в карточке нет блока cont_doc{text_number}")
    return _plain_text(node)
