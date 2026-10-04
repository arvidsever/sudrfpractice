"""КС РФ: разбор списка решений и дособор нового.

Образец — первые две строки настоящей страницы от 04.10.2026 с заменёнными
именами заявителей.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from harvester.ksrf import parse_list

FIXTURE = (Path(__file__).parent / "fixtures" / "ksrf_list.html").read_text(encoding="utf-8")


def test_list_row_and_page_count() -> None:
    rows, last = parse_list(FIXTURE)

    assert last == 6008
    first, second = rows
    assert first.pdf_id == 940667
    assert first.number == "57-П/2026"
    assert first.kind == "П"
    assert first.decision_date == date(2026, 9, 30)
    assert first.title.startswith("по делу о проверке конституционности")
    assert second.number == "56-П/2026"


class _Response:
    def __init__(self, text: str, url: str):
        self.text, self.content, self.url, self.status_code = text, text.encode(), url, 200


def test_catching_up_stops_at_the_first_familiar_page(db_settings, tmp_path) -> None:
    """После первого прохода — только новое: страницы с начала до первой,
    где всё уже известно. Новизну считать надо ДО записи — иначе любая
    страница после записи «знакомая», и дособор вставал бы на второй."""
    from sqlalchemy import create_engine

    from harvester.ksrf import Decision, _store, sweep_list
    from harvester.raw import RawStore

    engine = create_engine(db_settings.database_url)
    # Базу считаем заполненной: решений больше, чем (страниц − 1) × 10.
    _store(engine, [Decision(i, None, None, None, None) for i in range(1, 51)])

    def page(ids):
        rows = "".join(
            f'<li class="row"><span>01.10.2026</span><span>т</span>'
            f'<span><a href="/doc/KSRFDecision{i}.pdf">{i}-О/2026</a></span></li>'
            for i in ids
        )
        return f'<ol class="list-hover">{rows}</ol><a href="?PAGEN_1=5">»</a>'

    pages = {1: page([900001, 900002]), 2: page([900003, 5]), 3: page([6, 7]), 4: page([8, 9])}
    asked: list[int] = []

    class Client:
        def get(self, url: str):
            number = int(url.split("PAGEN_1=")[1]) if "PAGEN_1=" in url else 1
            asked.append(number)
            return _Response(pages[number], url)

    new = sweep_list(Client(), engine, RawStore(tmp_path))
    engine.dispose()

    assert new == 3, "три новых решения на первых двух страницах"
    assert asked == [1, 2, 3], "третья целиком знакомая — дальше не идём"
