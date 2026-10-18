"""КАД: разбор выдачи, отбор актов, окна поиска и документы — без сети."""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import create_engine, func, select

from harvester.kad import (
    fetch_docs,
    parse_search,
    sweep_search,
    wanted,
)

ROW = """
<tr><td class="num"><div class="b-container"><div class="civil" title="{day} 0:00:00">
<span>{day}</span></div><a href="https://kad.arbitr.ru/Card/{case_id}" class="num_case">
 {number} </a></div></td><td class="court"><div class="b-container">
<div class="judge" title="Иванова А. А.">Иванова А. А.</div></div></td>
<td class="plaintiff">Петров Пётр Петрович, ИНН 000000000000</td></tr>
"""


def page(total: int, rows: list[tuple[str, str, str]]) -> str:
    body = "".join(ROW.format(case_id=c, number=n, day=d) for c, n, d in rows)
    return f'<table>{body}</table><input type="hidden" id="documentsTotalCount" value="{total}" />'


def uid(i: int) -> str:
    return f"{i:08d}-0000-0000-0000-000000000000"


def test_search_row_keeps_case_but_not_sides() -> None:
    rows, total = parse_search(page(1, [(uid(1), "СИП-918/2026", "30.09.2026")]))
    assert total == 1
    row = rows[0]
    assert (row.case_id, row.number, row.case_kind) == (uid(1), "СИП-918/2026", "civil")
    assert row.registration_date == date(2026, 9, 30) and row.judge == "Иванова А. А."
    assert "Петров" not in repr(row), "стороны с ИНН не берём"


def test_only_final_acts_and_useful_rulings_are_wanted() -> None:
    assert wanted("Решения и постановления", "Постановление суда кассационной инстанции", "a.pdf")
    assert wanted("Определение", "О прекращении производства по делу", "a.pdf")
    assert not wanted("Определение", "О принятии кассационной жалобы к производству", "a.pdf")
    assert not wanted("Определение", "Об оставлении искового заявления без движения", "a.pdf")
    assert not wanted(
        "Прочие судебные документы", "Резолютивная часть решения суда первой инстанции", "a.pdf"
    )
    assert not wanted("Решения и постановления", "Решение суда первой инстанции", ""), "нет файла"


class _Response:
    def __init__(self, text: str):
        self.text, self.content, self.url, self.status_code = text, text.encode(), "u", 200


class FakeSession:
    """Выдача: 1 500 дел за месяц — больше потолка, и окно делится."""

    def __init__(self, cases: dict[date, int]):
        self.cases = cases  # день регистрации → сколько дел
        self.asked = 0

    def search(self, court, start, end, page_number=1):
        self.asked += 1
        days = [d for d in sorted(self.cases) if start <= d <= end]
        rows = [
            (uid(d.toordinal() * 1000 + i), f"А40-{i}/2026", f"{d:%d.%m.%Y}")
            for d in days
            for i in range(self.cases[d])
        ]
        chunk = rows[(page_number - 1) * 25 : page_number * 25]
        return _Response(page(len(rows) if court == "FASMO" else 0, chunk))


def test_window_over_the_cap_is_split_and_closed(db_settings, tmp_path) -> None:
    from harvester.db.schema import kad_case, kad_window
    from harvester.raw import RawStore

    engine = create_engine(db_settings.database_url)
    session = FakeSession({date(2026, 1, 5): 700, date(2026, 1, 25): 800})
    sweep_search(
        session, engine, RawStore(tmp_path), start=date(2026, 1, 1), today=date(2026, 1, 31)
    )
    with engine.connect() as connection:
        cases = connection.execute(select(func.count()).select_from(kad_case)).scalar_one()
        window = connection.execute(
            select(kad_window).where(kad_window.c.court_tag == "FASMO")
        ).one()
    engine.dispose()
    assert cases == 1500
    assert (window.window_from, window.total) == (date(2026, 1, 1), 1500)


def test_documents_of_our_instances_only(db_settings) -> None:
    from harvester.db.schema import kad_case, kad_doc

    engine = create_engine(db_settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            kad_case.insert().values(case_id=uid(1), number="А40-1/2025", court_tag="FASMO")
        )

    card = {
        "Result": {
            "CaseInfo": {
                "Instances": [
                    {"Id": "first", "InstanceLevel": 1, "Court": {"Tag": "MSK"}},
                    {"Id": "cass", "InstanceLevel": 3, "Court": {"Tag": "FASMO"}},
                ]
            }
        }
    }
    docs = {
        "Result": {
            "PagesCount": 1,
            "Items": [
                {
                    "Id": "d1",
                    "IsAct": True,
                    "DocumentTypeName": "Определение",
                    "ContentTypes": ["О принятии кассационной жалобы к производству"],
                    "FileName": "a.pdf",
                    "PublishDate": "/Date(1767225600000)/",
                },
                {
                    "Id": "d2",
                    "IsAct": True,
                    "DocumentTypeName": "Решения и постановления",
                    "ContentTypes": ["Постановление суда кассационной инстанции"],
                    "FinishInstance": 1,
                    "FileName": "b.pdf",
                    "PublishDate": "/Date(1770000000000)/",
                },
                {"Id": "d3", "IsAct": False, "DocumentTypeName": "Кассационная жалоба"},
            ],
        }
    }

    class Session:
        asked: list[str] = []

        def card(self, case_id):
            return _Response(json.dumps(card))

        def docs(self, instance_id, case_id, page_number=1):
            self.asked.append(instance_id)
            return _Response(json.dumps(docs))

    session = Session()
    assert fetch_docs(session, engine) == 2
    assert session.asked == ["cass"], "первая инстанция — не наш суд"
    with engine.connect() as connection:
        rows = {r.doc_id: r for r in connection.execute(select(kad_doc))}
    engine.dispose()
    assert set(rows) == {"d1", "d2"}
    assert rows["d2"].wanted and rows["d2"].finish_instance and not rows["d1"].wanted
    assert rows["d2"].publish_date.year == 2026
    assert fetch_docs(session, engine) == 0, "документы взяты, а инстанция закончилась"
