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


class Participant(BaseModel):
    """Лицо, участвующее в деле.

    Таблица называется по-разному в зависимости от производства:
    «УЧАСТНИКИ» у гражданских и КАС, «СТОРОНЫ» и «ЛИЦА» у уголовных,
    «СТОРОНЫ ПО ДЕЛУ» у КоАП. Содержимое тоже разное: у уголовных
    и КоАП есть перечень вменяемых статей и результат в отношении лица.

    В перечне стороны приходят одной строкой в третьей колонке; здесь они
    разложены по ролям, а у организаций есть ИНН, КПП, ОГРН.
    """

    role: str
    name: str
    #: «Перечень статей» — есть у уголовных и КоАП. Именно отсюда берутся
    #: нормы, за которыми иначе пришлось бы идти во внешний источник.
    articles: str | None = None
    #: «Результат в отношении лица» — только у уголовных.
    outcome: str | None = None
    inn: str | None = None
    kpp: str | None = None
    ogrn: str | None = None
    ogrnip: str | None = None


class Appeal(BaseModel):
    """Строка таблицы «ЖАЛОБЫ»: путь жалобы до рассмотрения.

    У кассации это половина сюжета — жалобу сперва изучает судья, и до
    заседания доходит не всякая.
    """

    filed_at: str | None = None
    applicant_status: str | None = None
    applicant: str | None = None
    passed_to_study_at: str | None = None
    with_case_request: str | None = None
    ruling_date: str | None = None
    study_result: str | None = None


class LowerCourt(BaseModel):
    """Рассмотрение в нижестоящем суде — связка кассации с первой инстанцией."""

    region: str | None = None
    court: str | None = None
    case_number: str | None = None
    decision_date: str | None = None


class Hearing(BaseModel):
    """Строка таблицы «СЛУШАНИЯ»: движение дела по событиям."""

    event: str
    date: str | None = None
    time: str | None = None
    place: str | None = None
    result: str | None = None
    published_at: str | None = None


class CaseCard(BaseModel):
    """Карточка дела целиком.

    Один запрос отдаёт больше, чем страница акта: сверх текста — участники
    с реквизитами, нижестоящий суд и движение. Поэтому свод текстов идёт
    через карточку, а не через `name_op=doc`.
    """

    case_uid: str | None = None
    receipt_date: str | None = None
    category: str | None = None
    appealed_act: str | None = None
    judge: str | None = None
    decision_date: str | None = None
    result: str | None = None
    appeal_result: str | None = None
    lower_court: LowerCourt | None = None
    participants: list[Participant] = Field(default_factory=list)
    hearings: list[Hearing] = Field(default_factory=list)
    appeals: list[Appeal] = Field(default_factory=list)
    #: Тексты актов по номеру вкладки: {1: "…", 2: "…"}. Пусто — акт
    #: не опубликован (262-ФЗ), это законное состояние.
    act_texts: dict[int, str] = Field(default_factory=dict)

    @property
    def has_act_text(self) -> bool:
        return any(text.strip() for text in self.act_texts.values())
