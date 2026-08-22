"""Разбор страницы сплошного перечня (`name_op=r`) — §4 грамматики.

Опорная точка — ссылка на карточку (`name_op=case`): из её href надёжно
достаются `case_id` и `case_uid` независимо от вёрстки. Привязка остальных
ячеек — позиционная, ровно восемь колонок; смена вёрстки суда потребует
новой фикстуры, поэтому число колонок проверяется явно.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urljoin, urlparse

from selectolax.parser import HTMLParser, Node

from ..guards import PageState, Verdict, check_page_completeness, classify
from ..models import ActLink, CaseRow, ListingPage

#: Восемь колонок: № дела, дата поступления, категория/стороны, судья,
#: дата решения, решение, дата вступления в силу, судебные акты.
COLUMN_COUNT = 8

_COL_RECEIPT = 1
_COL_ESSENCE = 2
_COL_JUDGE = 3
_COL_DECISION = 4
_COL_RESULT = 5
_COL_LEGAL_FORCE = 6


def _query_value(url: str, key: str) -> str | None:
    values = parse_qs(urlparse(url).query).get(key)
    return values[0] if values else None


def _cell_text(node: Node) -> str:
    return " ".join(node.text(separator=" ").split())


def _act_links(cell: Node, domain: str) -> list[ActLink]:
    links: list[ActLink] = []
    for anchor in cell.css("a"):
        href = anchor.attributes.get("href") or ""
        if "name_op=doc" not in href:
            continue
        number = _query_value(href, "number")
        if not number:
            continue
        raw_text_number = _query_value(href, "text_number")
        kind = anchor.attributes.get("TITLE") or anchor.attributes.get("title")
        links.append(
            ActLink(
                number=number,
                text_number=int(raw_text_number) if raw_text_number else 1,
                kind=kind or None,
                url=urljoin(f"https://{domain}/", href),
            )
        )
    return links


def parse_row(row: Node, domain: str) -> CaseRow | None:
    cells = row.css("td")
    if len(cells) < COLUMN_COUNT:
        return None

    anchor = None
    for candidate in cells[0].css("a"):
        href = candidate.attributes.get("href") or ""
        if "name_op=case" in href:
            anchor = candidate
            break
    if anchor is None:
        return None

    href = anchor.attributes.get("href") or ""
    number = " ".join(anchor.text().split())
    if not number:
        return None

    return CaseRow(
        case_number=number,
        receipt_date=_cell_text(cells[_COL_RECEIPT]) or None,
        essence=_cell_text(cells[_COL_ESSENCE]) or None,
        judge=_cell_text(cells[_COL_JUDGE]) or None,
        decision_date=_cell_text(cells[_COL_DECISION]) or None,
        result=_cell_text(cells[_COL_RESULT]) or None,
        legal_force_date=_cell_text(cells[_COL_LEGAL_FORCE]) or None,
        case_id=_query_value(href, "case_id"),
        case_uid=_query_value(href, "case_uid"),
        card_url=urljoin(f"https://{domain}/", href),
        act_links=_act_links(cells[COLUMN_COUNT - 1], domain),
    )


def column_titles(html: str) -> list[str]:
    """Заголовки колонок — чтобы заметить смену вёрстки раньше, чем она испортит корпус."""
    table = HTMLParser(html).css_first("table#tablcont")
    if table is None:
        return []
    header = table.css_first("tr")
    if header is None:
        return []
    return [" ".join(cell.text(separator=" ").split()) for cell in header.css("th")]


def parse_listing(
    html: str,
    *,
    court_domain: str,
    cartoteka_id: str,
    page: int = 1,
    state: PageState | None = None,
) -> ListingPage:
    """Разобрать страницу перечня, сверив число строк со счётчиком.

    Всё, кроме `Verdict.LISTING`, — исключение: разбирать не-выдачу нельзя,
    иначе кривой запрос молча превратится в «дел нет».
    """
    state = state or classify(html)
    if state.verdict is not Verdict.LISTING:
        raise ValueError(f"страница не является выдачей: {state.verdict.value}")

    table = HTMLParser(html).css_first("table#tablcont")
    assert table is not None  # гарантировано вердиктом LISTING

    rows: list[CaseRow] = []
    for node in table.css("tr"):
        parsed = parse_row(node, court_domain)
        if parsed is not None:
            rows.append(parsed)

    check_page_completeness(state, len(rows), page)
    assert state.total is not None  # проверено в check_page_completeness

    return ListingPage(
        court_domain=court_domain,
        cartoteka_id=cartoteka_id,
        page=page,
        total=state.total,
        rows=rows,
    )
