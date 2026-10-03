"""Справочники судов и картотек.

Списки ведутся в Swift-репозитории `Sudrf` и выгружаются оттуда командой
`swift run sudrf-cli export-directories`. Здесь мы их только читаем — руками
второй список не поддерживаем.

Три вещи справочник из Swift не знает, потому что приложению они не нужны;
они добавлены здесь и снабжены ссылкой на источник:

* какие суды закрыты капчей (§6 грамматики);
* префикс полей документа у каждой картотеки (§1);
* человекочитаемый ключ картотеки для CLI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "directories"

#: Суды, где на форме поиска стоит капча. Проверено живьём 06.08.2026
#: по наличию поля `captcha` в форме (§6 грамматики). У остальных шести
#: кассационных судов капчи нет.
CAPTCHA_COURTS: frozenset[str] = frozenset(
    {
        "1kas.sudrf.ru",
        "3kas.sudrf.ru",
        "4kas.sudrf.ru",
        "6kas.sudrf.ru",
        # Разведка 03.10.2026: из апелляционных капча у 2 АСОЮ и у
        # Апелляционного военного. Военный в справочник пока не внесён:
        # его капча приходит в JPEG, а решатель читает только PNG.
        "2ap.sudrf.ru",
    }
)

#: Префикс полей ДОКУМЕНТА для каждой картотеки. Он короче префикса дела
#: (`G3_DOCUMENT__` при `G33_CASE__`) и свой у каждой картотеки — подстановка
#: чужого выдачи не даёт. Значения заданы явно, а не выведены правилом:
#: это особенность платформы, и правило может однажды не сойтись.
#: `delo_id` для ПЕРЕЧНЯ. У гражданской и уголовной кассации он обязан быть
#: длинным: с короткой парой (`5`, `4`) портал отдаёт тот же набор дел и тот же
#: счётчик, но рисует страницу как АПЕЛЛЯЦИОННУЮ и оставляет колонку «Судебные
#: акты» пустой. Проверено 19.08.2026 — см. docs/delo-id-and-act-links.md.
#:
#: Реестр в `Sudrf` хранит короткие пары, и для приложения они верны: карточка
#: и поиск по номеру с ними работают. Расходится только перечень, поэтому
#: подстановка живёт здесь, а не в выгрузке.
LISTING_DELO_ID_BY_TABLE: dict[str, str] = {
    "g33_case": "2800001",
    "u33_case": "2450001",
    "p33_case": "43",
    "adm33_case": "2550001",
    # У АСОЮ короткая пара — родная: её ставят в форму поиска сами суды,
    # и по оси публикации с ней приходит ссылка на акт у каждой строки
    # (разведка 03.10.2026, 1/2/3/5 АСОЮ, все три картотеки).
    "g2_case": "5",
    "u2_case": "4",
    "p2_case": "42",
}

DOC_PREFIX_BY_TABLE: dict[str, str] = {
    "g33_case": "G3_DOCUMENT__",
    "u33_case": "U3_DOCUMENT__",
    "p33_case": "P3_DOCUMENT__",
    "adm33_case": "ADM3_DOCUMENT__",
    "g2_case": "G2_DOCUMENT__",
    "u2_case": "U2_DOCUMENT__",
    "p2_case": "P2_DOCUMENT__",
}


@dataclass(frozen=True, slots=True)
class Court:
    """Кассационный или апелляционный суд ОСЮ. `number` пуст у военных
    судов — они одни на страну и подсудности по субъектам не имеют."""

    domain: str
    title: str
    regions: tuple[str, ...]
    number: int | None = None
    #: `cassation` или `appeal`. Картотеки у уровней свои: у КСОЮ
    #: кассационные (`g3`…), у АСОЮ апелляционные (`g2`…), и чужая
    #: картотека суду не подходит — см. `cartoteki_for`.
    level: str = "cassation"

    @property
    def has_captcha(self) -> bool:
        return self.domain in CAPTCHA_COURTS


@dataclass(frozen=True, slots=True)
class Cartoteka:
    """Картотека КСОЮ: вид производства в кассационной инстанции.

    `new` важнее `delo_id`: у гражданской и уголовной кассации `new=0`
    тихо отдаёт форму поиска вместо выдачи (§1 и §5 грамматики).
    """

    id: str
    title: str
    delo_id: str
    new: str
    delo_table: str
    prefixes: tuple[str, ...]
    case_number_field: str
    uid_field: str
    name_field: str
    level: str = "cassation"

    @property
    def listing_delo_id(self) -> str:
        """`delo_id`, с которым перечень отдаёт ссылки на тексты актов."""
        try:
            return LISTING_DELO_ID_BY_TABLE[self.delo_table]
        except KeyError as exc:  # pragma: no cover — защита от нового delo_table
            raise KeyError(
                f"неизвестен delo_id перечня для {self.delo_table}; "
                "короткая пара из реестра молча лишает выдачу ссылок на акты"
            ) from exc

    @property
    def doc_prefix(self) -> str:
        try:
            return DOC_PREFIX_BY_TABLE[self.delo_table]
        except KeyError as exc:  # pragma: no cover — защита от нового delo_table
            raise KeyError(
                f"неизвестен префикс документа для {self.delo_table}; "
                "добавьте его в DOC_PREFIX_BY_TABLE, выводить правилом нельзя"
            ) from exc


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


#: Справочники по уровням: кассация выгружена из `Sudrf` утилитой,
#: апелляция перенесена из того же реестра вручную (см. поле `source`).
COURT_FILES = ("courts.json", "courts-appeal.json")
CARTOTEKA_FILES = ("cartoteki.json", "cartoteki-appeal.json")


@lru_cache(maxsize=1)
def courts() -> tuple[Court, ...]:
    return tuple(
        Court(
            domain=item["domain"],
            title=item["title"],
            regions=tuple(item["regions"]),
            number=item.get("number"),
            level=data["level"],
        )
        for name in COURT_FILES
        for data in [_load(name)]
        for item in data["courts"]
    )


@lru_cache(maxsize=1)
def cartoteki() -> tuple[Cartoteka, ...]:
    return tuple(
        Cartoteka(
            id=item["id"],
            title=item["title"],
            delo_id=item["delo_id"],
            new=item["new"],
            delo_table=item["delo_table"],
            prefixes=tuple(item["prefixes"]),
            case_number_field=item["case_number_field"],
            uid_field=item["uid_field"],
            name_field=item["name_field"],
            level=data["level"],
        )
        for name in CARTOTEKA_FILES
        for data in [_load(name)]
        for item in data["cartoteki"]
    )


def cartoteki_for(court: Court) -> tuple[Cartoteka, ...]:
    """Картотеки уровня этого суда. Все переборы «суд × картотека» идут
    через неё: кассационная картотека у апелляционного суда — это запрос,
    на который портал ответит формой или пустотой, и недосбор промолчит."""
    return tuple(item for item in cartoteki() if item.level == court.level)


def court(domain: str) -> Court:
    for item in courts():
        if item.domain == domain:
            return item
    raise KeyError(f"суд {domain} не найден в справочнике")


def cartoteka(cartoteka_id: str) -> Cartoteka:
    for item in cartoteki():
        if item.id == cartoteka_id:
            return item
    known = ", ".join(x.id for x in cartoteki())
    raise KeyError(f"картотека {cartoteka_id} не найдена; известны: {known}")
