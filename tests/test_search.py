"""Поиск по индексу.

Корпус собирается неделями, и поиск по нему работает на середине сбора.
Отсюда главное требование к нему: он не должен выглядеть полнее, чем есть.
«Найдено 12» без оговорки читается как «в природе двенадцать», хотя значит
«двенадцать в собранном на сегодня».
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, insert

from harvester.db.schema import case
from harvester.search import Query, run

CASES = [
    # (суд, картотека, номер, дата решения, судья, результат, акт)
    ("5kas.sudrf.ru", "g3", "88-1/2026", date(2026, 6, 1), "Белоусова Ю. К.", "ОТМЕНЕНО", True),
    ("5kas.sudrf.ru", "g3", "88-2/2026", date(2026, 7, 1), "Белоусова Ю. К.", "БЕЗ УДОВЛ.", True),
    ("5kas.sudrf.ru", "u3", "77-3/2026", date(2026, 5, 1), "Иванов И. И.", "БЕЗ УДОВЛ.", False),
    ("2kas.sudrf.ru", "g3", "88-4/2026", date(2026, 8, 1), "Петров П. П.", "ОТМЕНЕНО", None),
    # Нерассмотренное: даты решения нет, и в выдаче оно не должно быть первым.
    ("2kas.sudrf.ru", "g3", "88-5/2026", None, None, None, None),
]


def _seed(db_settings):
    engine = create_engine(db_settings.database_url)
    with engine.begin() as connection:
        for i, (court, cart, number, decided, judge, result, act) in enumerate(CASES):
            connection.execute(
                insert(case).values(
                    court_domain=court,
                    cartoteka_id=cart,
                    case_uid=f"uid-{i}",
                    case_number=number,
                    decision_date=decided,
                    judge=judge,
                    result=result,
                    act_published=act,
                )
            )
    return engine


def test_filters_narrow_together(db_settings) -> None:
    """Фасеты складываются через И: каждый следующий только сужает."""
    engine = _seed(db_settings)

    assert run(engine, Query(courts=("5kas.sudrf.ru",))).total == 3
    assert run(engine, Query(courts=("5kas.sudrf.ru",), cartoteki=("g3",))).total == 2
    assert (
        run(
            engine, Query(courts=("5kas.sudrf.ru",), cartoteki=("g3",), results=("ОТМЕНЕНО",))
        ).total
        == 1
    )
    engine.dispose()


def test_no_act_means_known_absent_not_unchecked(db_settings) -> None:
    """`act_published` трёхзначен, и `null` — это не `false`.

    «Текста нет» и «текст не проверяли» — разные состояния (262-ФЗ,
    этап 5.3). Фильтр, который их смешивает, обещает знание, которого нет.
    """
    engine = _seed(db_settings)

    assert run(engine, Query(with_act=True)).total == 2
    assert run(engine, Query(with_act=False)).total == 1, "только заведомо непубликуемое"
    assert run(engine, Query()).total == len(CASES), "без фильтра — все, включая непроверенные"
    engine.dispose()


def test_result_says_how_much_of_the_corpus_it_covers(db_settings) -> None:
    """Доля собранного — часть ответа, а не украшение."""
    engine = _seed(db_settings)

    found = run(engine, Query(courts=("5kas.sudrf.ru",)))
    assert found.collected_share == 3 / len(CASES)
    engine.dispose()


def test_newest_first_and_undecided_last(db_settings) -> None:
    """Практику ищут от нового к старому. Дела без даты решения — не самые
    свежие, а ещё не рассмотренные, и первыми им быть незачем."""
    engine = _seed(db_settings)

    numbers = [row["case_number"] for row in run(engine, Query()).rows]
    assert numbers[0] == "88-4/2026", "самое свежее решение вперёд"
    assert numbers[-1] == "88-5/2026", "нерассмотренное — в конец"
    engine.dispose()


def test_facets_count_inside_the_filter(db_settings) -> None:
    """Счётчик фасета обязан считать в уже суженной выдаче, иначе он врёт:
    «судья Иванов — 1» рядом с фильтром по гражданским, где его нет."""
    engine = _seed(db_settings)

    found = run(engine, Query(cartoteki=("g3",)), with_facets=True)
    judges = dict(found.facets["судья"])

    assert "Иванов И. И." not in judges, "судья уголовной картотеки в фасете гражданской"
    assert judges["Белоусова Ю. К."] == 2
    engine.dispose()


TEXTS = {
    # номер дела -> текст акта
    "88-1/2026": (
        "Суд кассационной инстанции полагает, что неустойка явно несоразмерна "
        "последствиям нарушения обязательства, в связи с чем подлежит снижению."
    ),
    "88-2/2026": (
        "Доводы о пропуске срока исковой давности отклонены: течение срока "
        "было прервано признанием долга."
    ),
}


def _seed_texts(engine):
    """Положить тексты двум делам. `tsv` считает сама база — колонка
    generated, и в этом весь смысл проверки: индекс и запрос обязаны
    ходить одним словарём."""
    from sqlalchemy import insert, select

    from harvester.db.schema import act, act_text

    with engine.begin() as connection:
        for number, text in TEXTS.items():
            case_pk = connection.execute(
                select(case.c.id).where(case.c.case_number == number)
            ).scalar_one()
            act_id = connection.execute(
                insert(act).values(case_pk=case_pk, text_number=1).returning(act.c.id)
            ).scalar_one()
            connection.execute(insert(act_text).values(act_id=act_id, plain_text=text))


def test_text_search_finds_by_word_form_not_by_substring(db_settings) -> None:
    """Полнотекст ищет словами, а не подстрокой: «несоразмерность»
    в запросе обязана найти «несоразмерна» в тексте. Это и делает русский
    словарь в `tsvector`; на `ilike` такой запрос не нашёл бы ничего."""
    engine = _seed(db_settings)
    _seed_texts(engine)

    found = run(engine, Query(text="несоразмерность неустойки"))

    assert found.total == 1
    assert found.rows[0]["case_number"] == "88-1/2026"
    engine.dispose()


def test_text_search_composes_with_facets(db_settings) -> None:
    """Слова — такое же сужение, как суд или дата, и складываются с ними."""
    engine = _seed(db_settings)
    _seed_texts(engine)

    assert run(engine, Query(text="срок давности")).total == 1
    assert run(engine, Query(text="срок давности", results=("ОТМЕНЕНО",))).total == 0
    engine.dispose()


def test_a_case_with_two_matching_acts_is_found_once(db_settings) -> None:
    """Дело — единица выдачи, акт — нет.

    У дела бывает несколько актов, и соединение вместо `EXISTS` размножило бы
    дело по числу совпавших текстов: «найдено 2» там, где дело одно.
    """
    from sqlalchemy import insert, select

    from harvester.db.schema import act, act_text

    engine = _seed(db_settings)
    _seed_texts(engine)
    with engine.begin() as connection:
        case_pk = connection.execute(
            select(case.c.id).where(case.c.case_number == "88-1/2026")
        ).scalar_one()
        act_id = connection.execute(
            insert(act).values(case_pk=case_pk, text_number=2).returning(act.c.id)
        ).scalar_one()
        connection.execute(
            insert(act_text).values(act_id=act_id, plain_text="Неустойка несоразмерна также.")
        )

    found = run(engine, Query(text="несоразмерна"))

    assert found.total == 1, "дело с двумя совпавшими актами — одно дело"
    assert len(found.rows) == 1
    engine.dispose()


def test_snippet_shows_why_it_matched(db_settings) -> None:
    """Без куска текста выдача не отвечает на вопрос «за что нашлось»."""
    engine = _seed(db_settings)
    _seed_texts(engine)

    row = run(engine, Query(text="несоразмерность")).rows[0]

    assert "«" in row["snippet"], f"совпадение должно быть выделено: {row['snippet']}"
    assert "несоразмерна" in row["snippet"]
    engine.dispose()


def test_texts_share_is_reported_only_when_asked_about_texts(db_settings) -> None:
    """Потолок полнотекста — доля разобранных актов, и он не равен полноте
    индекса. Ссылок на акты 1,6 млн, текстов 375 тысяч: поиск по словам
    видит четверть того, что видит поиск по реквизитам, и молчать об этом
    нельзя. Но и считать лишнего при обычном запросе незачем."""
    engine = _seed(db_settings)
    _seed_texts(engine)

    обычный = run(engine, Query())
    словами = run(engine, Query(text="неустойка"))

    assert обычный.texts_share == 0.0, "при обычном запросе тексты ни при чём"
    assert словами.texts_share == 1.0

    # И обратно: полный счёт корпуса — 2,9 с на 2,75 млн строк, и при
    # поиске по словам он не нужен, потому что потолок задают не дела.
    assert словами.collected_share == 0.0, "лишний счёт корпуса не оплачиваем"
    assert обычный.collected_share > 0
    engine.dispose()
