"""Модели разобранных страниц.

Форма повторяет `CaseSearchResult`/`CaseActLink` из `SudrfKit` — там она
выверена по живым страницам. Даты остаются строками ровно в том виде,
в каком их отдал суд: разбор в `date` — дело слоя записи в БД, и он не должен
происходить там, где ошибку не на что списать.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ActLink(BaseModel):
    """Ссылка на текст судебного акта из последней колонки строки выдачи."""

    #: `number=` — идентификатор документа в базе суда.
    number: str
    #: `text_number=` — порядковый номер акта внутри дела, с 1.
    text_number: int = 1
    #: Ярлык из `TITLE`: «Постановления», «Решения», «Определение».
    kind: str | None = None
    #: `delo_id`/`new` из самой ссылки — портал ставит там длинную пару.
    delo_id: str | None = None
    new: str | None = None
    url: str


class CaseRow(BaseModel):
    """Одна строка перечня. Самодостаточна: реквизиты берутся без карточки."""

    case_number: str
    receipt_date: str | None = None
    essence: str | None = None
    judge: str | None = None
    decision_date: str | None = None
    result: str | None = None
    legal_force_date: str | None = None
    case_id: str | None = None
    case_uid: str | None = None
    card_url: str | None = None
    #: Пусто — законное состояние: по 262-ФЗ публикуется не всё.
    act_links: list[ActLink] = Field(default_factory=list)

    @property
    def has_published_act(self) -> bool:
        return bool(self.act_links)


class ListingPage(BaseModel):
    """Страница перечня вместе со счётчиком — счётчик и есть мера полноты."""

    court_domain: str
    cartoteka_id: str
    page: int
    total: int
    rows: list[CaseRow]


class ActText(BaseModel):
    """Текст акта со страницы `name_op=doc`."""

    number: str
    text_number: int
    text: str

    @property
    def is_empty_document(self) -> bool:
        """«ПУСТОЙ ДОКУМЕНТ» — признак конца перебора `text_number`, не ошибка."""
        return "ПУСТОЙ ДОКУМЕНТ" in self.text
