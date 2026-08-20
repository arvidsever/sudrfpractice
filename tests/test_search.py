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
