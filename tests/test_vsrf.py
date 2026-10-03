"""ВС РФ: разбор выдачи и обход окнами.

Образцы — настоящие строки выдачи от 03.10.2026: два акта Апелляционной
коллегии и одно производство-«дело» с заменёнными участниками и судьёй.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from harvester.vsrf import listing_page, months, parse_acts, parse_claims

FIXTURES = Path(__file__).parent / "fixtures"


def _page(name: str):
    return listing_page((FIXTURES / name).read_text(encoding="utf-8"))


def test_listing_comes_from_next_data() -> None:
    page = _page("vsrf_acts.html")
    assert page.total == 2
    assert page.last is True


def test_page_without_listing_is_an_error_not_zero() -> None:
    """Сменись устройство сайта, пустая выдача молча стала бы «актов нет»."""
    with pytest.raises(ValueError):
        listing_page("<html><body>Электронная справочная</body></html>")


def test_act_row_is_read_by_labels() -> None:
    first, second = parse_acts(_page("vsrf_acts.html").content)

    assert first.pdf_id == 2566920
    assert first.number == "АПЛ26-207"
    assert first.claim_id == "19-37056469"
    assert first.act_kind == "Определение"
    assert first.act_date == date(2026, 9, 10)
    assert first.instance == "Апелляция"
    assert first.collegium == "Апелляционная коллегия"
    assert first.judge == "Зинченко И.Н."
    assert first.subject.startswith("о признании частично недействующими")
    assert second.number == "АПЛ26-204"


def test_claim_row_carries_what_links_it_to_our_corpus() -> None:
    """УИД и номер дела первой инстанции — ради них выдача «дел» и берётся.
    Двоеточие внутри значения («…Судья: …») подписью не считается."""
    (claim,) = parse_claims(_page("vsrf_claims.html").content)

    assert claim.claim_id == "12-37210385"
    assert claim.number == "85-КГ26-5-К1"
    assert claim.received_date == date(2026, 9, 30)
    assert claim.case_uid == "40RS0011-03-2025-000262-05"
    assert claim.first_case_number == "2-3-235/2025"
    assert claim.first_court.startswith("Козельский районный суд")
    assert "Судья" in claim.first_court
    assert claim.subject == "о признании недействительным пункта договора аренды земельного участка"


def test_months_cover_the_range_without_gaps() -> None:
    windows = list(months(date(2024, 1, 15), date(2024, 3, 10)))
    assert windows == [
        (date(2024, 1, 1), date(2024, 1, 31)),
        (date(2024, 2, 1), date(2024, 2, 29)),
        (date(2024, 3, 1), date(2024, 3, 10)),
    ]


class _Response:
    def __init__(self, text: str, url: str):
        self.text = text
        self.content = text.encode("utf-8")
        self.url = url
        self.status_code = 200


class _Client:
    """Отдаёт одну и ту же выдачу на любой запрос, считая запросы."""

    def __init__(self, text: str):
        self.text = text
        self.asked: list[str] = []

    def get(self, url: str):
        self.asked.append(url)
        return _Response(self.text, url)


def test_window_is_closed_only_when_it_matches_the_counter(db_settings, tmp_path) -> None:
    """Окно, где собрано меньше, чем обещал сайт, остаётся открытым и будет
    пройдено снова. Так же ловится недосбор у КСОЮ: сверкой со счётчиком."""
    from sqlalchemy import create_engine, func, select

    from harvester.db.schema import vsrf_act, vsrf_window
    from harvester.raw import RawStore
    from harvester.vsrf import sweep_listing

    engine = create_engine(db_settings.database_url)
    store = RawStore(tmp_path)
    honest = (FIXTURES / "vsrf_acts.html").read_text(encoding="utf-8")
    short = honest.replace('"totalElements": 2', '"totalElements": 3')

    window = {"start": date(2026, 9, 1), "today": date(2026, 9, 30)}
    sweep_listing("acts", _Client(short), engine, store, **window)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(vsrf_window)).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(vsrf_act)).scalar_one() == 2

    sweep_listing("acts", _Client(honest), engine, store, **window)
    with engine.connect() as connection:
        closed = connection.execute(select(func.count()).select_from(vsrf_window)).scalar_one()
    assert closed == 5, "все пять видов судопроизводства за сентябрь сошлись со счётчиком"
    engine.dispose()


ECONOMIC_ROW = """
<div class="CaseStyle_case_item__x"><a href="http://kad.arbitr.ru/Kad/Card?number=А40-1%2F2025">
305-ЭС26-1</a><a href="/lk/practice/stor_pdf_ec/2570944">Определение</a><span>10.09.2026</span>
<span>Вид судопроизводства:</span><span>Административное судопроизводство</span>
<span>Судебная коллегия:</span><span>Судебная коллегия по экономическим спорам</span></div>
"""


def test_economic_collegium_is_counted_but_not_kept(db_settings, tmp_path) -> None:
    """Владелец решил экономическую коллегию не брать, а «административная»
    выдача сайта наполовину из неё: дела по главе 24 АПК. Её строки обязаны
    войти в сверку со счётчиком — иначе окно не закроется никогда, — но в базу
    не попадают. 03.10.2026 выдача за сентябрь открылась страницей, где все
    двадцать строк экономические, и сбор остановился на ней, не дойдя до наших."""
    import json

    from sqlalchemy import create_engine, func, select

    from harvester.db.schema import vsrf_act, vsrf_window
    from harvester.raw import RawStore
    from harvester.vsrf import sweep_listing

    ours = _page("vsrf_acts.html").content
    data = {
        "props": {
            "pageProps": {
                "initialItemsData": {
                    "content": ECONOMIC_ROW + ours,
                    "totalElements": 3,
                    "last": True,
                }
            }
        }
    }
    page = f'<script id="__NEXT_DATA__">{json.dumps(data, ensure_ascii=False)}</script>'

    engine = create_engine(db_settings.database_url)
    sweep_listing(
        "acts",
        _Client(page),
        engine,
        RawStore(tmp_path),
        start=date(2026, 9, 1),
        today=date(2026, 9, 30),
    )
    with engine.connect() as connection:
        kept = connection.execute(select(func.count()).select_from(vsrf_act)).scalar_one()
        closed = connection.execute(select(func.count()).select_from(vsrf_window)).scalar_one()
    engine.dispose()

    assert kept == 2, "экономическая строка в базу не попадает"
    assert closed == 5, "но в сверку входит: три строки на счётчик три — окно закрыто"


def test_supreme_court_pages_are_utf8_and_gas_pages_cp1251() -> None:
    """Заголовком кодировку не объявляет ни ГАС, ни ВС. Прочитанная как cp1251,
    выдача ВС превратила номера в «РђРџР›26-18Р”», и у всех «дел» пропали УИД
    и даты — подписи полей не находились."""
    from harvester.http import Response

    word = "Инстанция:"
    vsrf = Response(
        url="https://www.vsrf.ru/lk/practice/claims", status_code=200, content=word.encode("utf-8")
    )
    gas = Response(
        url="https://5kas.sudrf.ru/modules.php", status_code=200, content=word.encode("cp1251")
    )
    assert vsrf.text == word
    assert gas.text == word


def test_long_claim_id_does_not_break_the_sweep(db_settings) -> None:
    """У дисциплинарных дел идентификатор карточки в 38 знаков; колонка
    на 32 уронила сбор «дел» на первой же странице."""
    from sqlalchemy import create_engine, select

    from harvester.db.schema import vsrf_claim
    from harvester.vsrf import ClaimRow, _store_claims

    long_id = "1-10-3F834369BAAB45BFB3E06A8E1E034938"
    engine = create_engine(db_settings.database_url)
    _store_claims(
        engine,
        "DISCIPLINARY_DISPUTE",
        [
            ClaimRow(
                claim_id=long_id,
                economic=False,
                number="ДК26-152",
                received_date=None,
                instance=None,
                case_uid=None,
                first_court=None,
                first_case_number=None,
                subject=None,
            )
        ],
    )
    with engine.connect() as connection:
        stored = connection.execute(select(vsrf_claim.c.claim_id)).scalar_one()
    engine.dispose()
    assert stored == long_id


def test_arbitration_claim_is_counted_but_not_kept() -> None:
    """В выдаче «дел» по КоАП за сентябрь 2026 — 49 арбитражных из 121:
    ссылка в картотеку арбитражных судов вместо карточки ВС. Без них сверка
    со счётчиком не сходилась, и окно не закрывалось."""
    from harvester.vsrf import parse_claims

    row = """<div class="CaseStyle_case_item__x">
        <a href="http://kad.arbitr.ru/Kad/Card?number=А40-252024%2F2022">303-АД26-1</a>
        <span>Дата поступления:</span><span>05.09.2026</span></div>"""
    (claim,) = parse_claims(row)

    assert claim.economic is True
    assert claim.claim_id == "kad:303-АД26-1", "ключ — номер производства ВС, не ссылка"


def test_unstable_paging_is_cured_by_a_narrower_window(db_settings, tmp_path) -> None:
    """Сайт листает нестабильно: за сентябрь 2026 по КоАП три строки попали
    на две страницы, три другие — ни на одну. Окно, которое не сошлось,
    делится пополам по датам, пока половины не сойдутся со счётчиком."""
    import json
    import re as regex

    from sqlalchemy import create_engine, func, select

    from harvester.db.schema import vsrf_act, vsrf_window
    from harvester.raw import RawStore
    from harvester.vsrf import _items, sweep_listing

    first, second = (
        '<div class="CaseStyle_case_item__' + part
        for part in _page("vsrf_acts.html").content.split('<div class="CaseStyle_case_item__')[1:]
    )

    def page(content: str, total: int) -> str:
        data = {
            "props": {
                "pageProps": {
                    "initialItemsData": {"content": content, "totalElements": total, "last": True}
                }
            }
        }
        return f'<script id="__NEXT_DATA__">{json.dumps(data, ensure_ascii=False)}</script>'

    class Flaky:
        def get(self, url: str):
            start, end = regex.findall(r"actDate(?:From|To)=([\d.]+)", url)
            if "CIVIL" not in url:
                return _Response(page("", 0), url)
            if (start, end) == ("01.09.2026", "30.09.2026"):
                return _Response(page(first + first, 2), url)  # плывущая выдача
            if end == "15.09.2026":
                return _Response(page(first, 1), url)
            return _Response(page(second, 1), url)

    assert len(_items(first + second)) == 2
    engine = create_engine(db_settings.database_url)
    sweep_listing(
        "acts", Flaky(), engine, RawStore(tmp_path), start=date(2026, 9, 1), today=date(2026, 9, 30)
    )
    with engine.connect() as connection:
        kept = connection.execute(select(func.count()).select_from(vsrf_act)).scalar_one()
        civil = connection.execute(
            select(vsrf_window.c.total).where(vsrf_window.c.case_type == "CIVIL")
        ).scalar_one()
    engine.dispose()
    assert kept == 2, "обе строки собраны — по половинам месяца"
    assert civil == 2, "окно закрыто"
