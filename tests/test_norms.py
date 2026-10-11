"""Ссылки на нормы: «практика по ст. 333 ГК» — запрос, а не поиск слов."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, insert, select

from harvester.db.schema import act, act_text, case, ksrf_decision, ksrf_decision_text
from harvester.norms import backfill, extract, parse_query
from harvester.search import Query, run, run_higher


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("на основании ст. 333 ГК РФ суд", ["ГК:333"]),
        ("статьи 333 Гражданского кодекса Российской Федерации", ["ГК:333"]),
        ("п. 1 ст.333 ГК", ["ГК:333"]),
        ("статьями 15, 393 и 1064 ГК РФ", ["ГК:1064", "ГК:15", "ГК:393"]),
        ("ст.ст. 131-132 ГПК РФ", ["ГПК:131", "ГПК:132"]),
        ("статьи 56 Гражданского процессуального кодекса", ["ГПК:56"]),
        ("ч. 1 ст. 12.8 КоАП РФ", ["КоАП:12.8"]),
        (
            "статьёй 30.7 Кодекса Российской Федерации об административных правонарушениях",
            ["КоАП:30.7"],
        ),
        ("статьи 401.15 Уголовно-процессуального кодекса", ["УПК:401.15"]),
        ("статьи 46 Конституции Российской Федерации", ["Конституция:46"]),
        (
            "статьи 7, 19 (часть 1), 40 (части 1 и 3) и 55 (часть 3) Конституции",
            ["Конституция:19", "Конституция:40", "Конституция:55", "Конституция:7"],
        ),
        ("статьей 125 (пункт «а» части 4) Конституции Российской Федерации", ["Конституция:125"]),
        # Чего НЕ берём: кодекс не назван, или это вовсе не кодекс.
        ("статьи 333 названного Кодекса", []),
        ("статьи 5 Указа Президента", []),
        ("взыскано 333 рубля, ГК тут ни при чём", []),
    ],
)
def test_references_are_normalized(text: str, expected: list[str]) -> None:
    assert extract(text) == expected


def test_query_is_read_more_loosely_than_text() -> None:
    assert parse_query("333 гк рф") == ["ГК:333"]
    assert parse_query("ст. 12.8 КоАП") == ["КоАП:12.8"]
    assert parse_query("неустойка") == []


def test_norm_search_finds_the_act_and_ranks_by_density(db_settings) -> None:
    engine = create_engine(db_settings.database_url)
    texts = {
        # Старое дело — про неустойку целиком; свежее — упоминает вскользь.
        "88-1/2025": (
            date(2025, 1, 1),
            "Неустойка несоразмерна. Суд снизил неустойку по ст. 333 ГК РФ.",
        ),
        "88-2/2026": (
            date(2026, 1, 1),
            "Спор о границах участка по статье 60 ЗК РФ. "
            + "Межевание проведено. " * 40
            + "Неустойка не заявлялась.",
        ),
    }
    with engine.begin() as connection:
        for number, (decided, body) in texts.items():
            case_pk = connection.execute(
                insert(case)
                .values(
                    court_domain="5kas.sudrf.ru",
                    cartoteka_id="g3",
                    case_uid=number,
                    case_number=number,
                    decision_date=decided,
                )
                .returning(case.c.id)
            ).scalar_one()
            act_id = connection.execute(
                insert(act).values(case_pk=case_pk, text_number=1).returning(act.c.id)
            ).scalar_one()
            connection.execute(insert(act_text).values(act_id=act_id, plain_text=body))
        connection.execute(insert(ksrf_decision).values(pdf_id=7, number="57-П/2026"))
        connection.execute(
            insert(ksrf_decision_text).values(pdf_id=7, plain_text="по статье 333 ГК РФ")
        )

    assert backfill(engine) == {"act": 2, "vsrf": 0, "ksrf": 1}
    assert backfill(engine) == {"act": 0, "vsrf": 0, "ksrf": 0}, "второй проход — пустой"

    by_norm = run(engine, Query(norms=("ГК:333",)))
    assert [r["case_number"] for r in by_norm.rows] == ["88-1/2025"]
    assert run(engine, Query(norms=("ГК:333", "ЗК:60"))).total == 0, "нормы — через И"
    assert run_higher(engine, Query(norms=("ГК:333",)), "www.ksrf.ru").total == 1

    relevant = run(engine, Query(text="неустойка"))
    assert [r["case_number"] for r in relevant.rows] == ["88-1/2025", "88-2/2026"]
    recent = run(engine, Query(text="неустойка", sort="date"))
    assert [r["case_number"] for r in recent.rows] == ["88-2/2026", "88-1/2025"]
    with engine.connect() as connection:
        assert connection.execute(select(case.c.id)).all()
    engine.dispose()
