"""ВС РФ: тексты судебных актов и производства-«дела» (docs/vsrf.md).

Сайт ВС устроен иначе, чем ГАС «Правосудие», и проще:

* выдача лежит прямо в странице, в `<script id="__NEXT_DATA__">`
  (`props.pageProps.initialItemsData` — HTML строк, счётчик и признак
  последней страницы); скрытый `/api/` не нужен, и `robots.txt` его
  как раз закрывает;
* листание — параметр `page` в адресе, с нуля, по 20 строк;
* капчи нет, ответы в UTF-8.

Строки разбираются по подписям полей («Инстанция:», «Уникальный
идентификатор дела:»), а не по CSS-классам: у тех хеш-суффиксы
(`CaseStyle_case_item__dMbr2`), и меняются они при каждой выкладке сайта.

Экономическая коллегия не собирается — решение владельца 03.10.2026:
это две трети актов, а нам нужна общая юрисдикция.
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from selectolax.parser import HTMLParser, Node
from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert

from .config import Settings
from .config import settings as default_settings
from .db.schema import vsrf_act, vsrf_act_text, vsrf_claim, vsrf_window
from .db.store import save_raw_page
from .raw import RawStore

log = logging.getLogger("harvester.vsrf")

BASE = "https://www.vsrf.ru/lk/practice"
#: В сырье и в счётчиках запросов ВС живёт под этим именем.
DOMAIN = "www.vsrf.ru"

#: Виды судопроизводства без экономических споров.
CASE_TYPES = (
    "CIVIL",
    "CRIMINAL",
    "ADMINISTRATIVE",
    "ADMINISTRATIVE_INFRACTION",
    "DISCIPLINARY_DISPUTE",
)

#: Откуда начинать окна. Проверено 03.10.2026: раньше актов нет.
START = date(2005, 1, 1)

#: Свежие окна перепроверяются каждый прогон: ВС публикует с задержкой,
#: и закрытый вчера месяц сегодня может прирасти актами.
RECHECK_DAYS = 120

MAX_RUN_HOURS = 6.0

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_DATE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")


# --- разбор ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListingPage:
    content: str
    total: int | None
    last: bool


def listing_page(html: str) -> ListingPage:
    """Достать выдачу из страницы. Нет выдачи — ошибка, а не «ноль актов»:
    иначе смена устройства сайта молча превратится в пустой корпус."""
    match = _NEXT_DATA.search(html)
    if match is None:
        raise ValueError("в странице нет __NEXT_DATA__ — сайт сменил устройство")
    items = json.loads(match.group(1))["props"]["pageProps"].get("initialItemsData")
    if items is None:
        raise ValueError("в странице нет выдачи (initialItemsData)")
    return ListingPage(
        content=items.get("content") or "",
        total=items.get("totalElements"),
        last=bool(items.get("last")),
    )


def _segments(node: Node) -> list[str]:
    out = []
    for item in node.traverse(include_text=True):
        if item.tag == "-text":
            text = " ".join((item.text_content or "").split())
            if text:
                out.append(text)
    return out


#: Подписи, у которых значение бывает в той же текстовой ноде:
#: «Номер дела 1-й инстанции: 2-3-235/2025».
_INLINE = ("Номер дела 1-й инстанции",)


def _fields(segments: list[str]) -> dict[str, str]:
    """Пары «подпись: значение». Подпись — сегмент, оканчивающийся
    двоеточием; значение — следующий. Двоеточие внутри значения
    («…Судья: О.В.Алексеева») подписью не считается."""
    fields: dict[str, str] = {}
    for i, segment in enumerate(segments):
        for label in _INLINE:
            if segment.startswith(label + ":") and segment != label + ":":
                fields.setdefault(label, segment.split(":", 1)[1].strip())
        if segment.endswith(":") and i + 1 < len(segments):
            value = segments[i + 1]
            if not value.endswith(":"):
                fields.setdefault(segment[:-1], value)
    return fields


def _subject(fields: dict[str, str]) -> str | None:
    """Предмет: «По иску:», «По заявлению:», «По жалобе:» — подпись зависит
    от вида дела, а смысл один."""
    for label, value in fields.items():
        if label.startswith("По "):
            return value
    return None


def _date(value: str | None) -> date | None:
    match = _DATE.search(value or "")
    if match is None:
        return None
    day, month, year = (int(x) for x in match.groups())
    return date(year, month, day)


def _items(content: str) -> list[Node]:
    return HTMLParser(content).css('div[class*="case_item__"]')


def _link(node: Node, prefix: str) -> tuple[str, str] | None:
    for anchor in node.css("a[href]"):
        href = anchor.attributes.get("href") or ""
        if href.startswith(prefix):
            return href[len(prefix) :].strip("/"), " ".join(anchor.text().split())
    return None


@dataclass(frozen=True, slots=True)
class ActRow:
    pdf_id: int
    #: Экономическая коллегия: PDF в `/stor_pdf_ec/`, вместо карточки ВС —
    #: ссылка в картотеку арбитражных судов. Владелец решил её не брать
    #: (03.10.2026), но выдача «административных» дел наполовину из неё
    #: и состоит — дела по главе 24 АПК. Такие строки считаются при сверке
    #: со счётчиком сайта и не сохраняются.
    economic: bool
    number: str | None
    claim_id: str | None
    act_kind: str | None
    act_date: date | None
    instance: str | None
    subject: str | None
    collegium: str | None
    judge: str | None


def parse_acts(content: str) -> list[ActRow]:
    rows = []
    for item in _items(content):
        pdf = _link(item, "/lk/practice/stor_pdf/")
        economic = pdf is None
        if economic:
            pdf = _link(item, "/lk/practice/stor_pdf_ec/")
        if pdf is None:
            # Акт без файла: искать нечего. Не ошибка — но и не строка корпуса.
            continue
        claim = _link(item, "/lk/practice/claims/")
        segments = _segments(item)
        fields = _fields(segments)
        economic = economic or "экономическим спорам" in (fields.get("Судебная коллегия") or "")
        rows.append(
            ActRow(
                pdf_id=int(pdf[0]),
                economic=economic,
                number=claim[1] if claim else None,
                claim_id=claim[0] if claim else None,
                act_kind=pdf[1] or None,
                act_date=next((d for d in map(_date, segments) if d), None),
                instance=fields.get("Инстанция"),
                subject=_subject(fields),
                collegium=fields.get("Судебная коллегия"),
                judge=fields.get("Судья-докладчик"),
            )
        )
    return rows


@dataclass(frozen=True, slots=True)
class ClaimRow:
    claim_id: str
    #: Арбитражное дело: вместо карточки ВС — ссылка в картотеку арбитражных
    #: судов. В выдаче КоАП таких 49 из 121 за сентябрь 2026 (дела по главе 25
    #: АПК). Как и у актов: при сверке считаются, в базу не попадают.
    economic: bool
    number: str | None
    received_date: date | None
    instance: str | None
    case_uid: str | None
    first_court: str | None
    first_case_number: str | None
    subject: str | None


def parse_claims(content: str) -> list[ClaimRow]:
    """Строки выдачи производств. Участников не берём: для связи с нашим
    корпусом нужен УИД, а ФИО — лишние персональные данные."""
    rows = []
    for item in _items(content):
        claim = _link(item, "/lk/practice/claims/")
        economic = claim is None
        if economic:
            claim = _link(item, "http://kad.arbitr.ru/") or _link(item, "https://kad.arbitr.ru/")
            if claim is None:
                continue
            claim = ("kad:" + claim[0], claim[1])
        fields = _fields(_segments(item))
        rows.append(
            ClaimRow(
                claim_id=claim[0],
                economic=economic,
                number=claim[1] or None,
                received_date=_date(fields.get("Дата поступления")),
                instance=fields.get("Инстанция"),
                case_uid=fields.get("Уникальный идентификатор дела"),
                first_court=fields.get("Суд 1-й инстанции"),
                first_case_number=fields.get("Номер дела 1-й инстанции"),
                subject=_subject(fields),
            )
        )
    return rows


def pdf_text(content: bytes) -> str:
    """Текстовый слой PDF. Акты ВС — сканы с распознанным текстом поверх,
    и вынимается он целиком (docs/vsrf.md). Пустая строка — слоя нет:
    это кандидат на распознавание потом, а не «акта нет»."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    # PostgreSQL не хранит NUL в text, а распознанный слой их иногда несёт.
    return text.replace("\x00", "")


# --- адреса ----------------------------------------------------------------


def _fmt(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def acts_url(case_type: str, start: date, end: date, page: int = 0) -> str:
    url = (
        f"{BASE}/acts?actDateFrom={_fmt(start)}&actDateTo={_fmt(end)}"
        f"&actDateExact=false&caseType={case_type}"
    )
    return url + (f"&page={page}" if page else "")


def claims_url(case_type: str, start: date, end: date, page: int = 0) -> str:
    """Только производства-«дела»: у них есть УИД. Жалобы — миллионы строк
    без УИД и без опубликованных актов, связать их надёжно нельзя."""
    url = (
        f"{BASE}/claims?registerDateExact=off&considerationDateExact=off&numberExact=true"
        f"&registerDateFrom={_fmt(start)}&registerDateTo={_fmt(end)}"
        f"&caseType={case_type}&claimType=CASE"
    )
    return url + (f"&page={page}" if page else "")


def pdf_url(pdf_id: int) -> str:
    return f"{BASE}/stor_pdf/{pdf_id}"


def months(start: date, end: date):
    current = start.replace(day=1)
    while current <= end:
        following = (current + timedelta(days=32)).replace(day=1)
        yield current, min(following - timedelta(days=1), end)
        current = following


# --- сбор ------------------------------------------------------------------


def _save_raw(engine: Engine, store: RawStore, response, kind: str) -> int:
    record = store.save(
        response.content,
        url=str(response.url),
        court_domain=DOMAIN,
        http_status=response.status_code,
        content_kind=kind,
        extension="pdf" if kind == "vsrf_act_pdf" else "html",
    )
    with engine.begin() as connection:
        return save_raw_page(connection, record)


def _store_acts(engine: Engine, case_type: str, rows: list[ActRow]) -> None:
    rows = [row for row in rows if not row.economic]
    if not rows:
        return
    values = [
        {
            "pdf_id": r.pdf_id,
            "number": r.number,
            "claim_id": r.claim_id,
            "act_kind": r.act_kind,
            "act_date": r.act_date,
            "case_type": case_type,
            "instance": r.instance,
            "subject": r.subject,
            "collegium": r.collegium,
            "judge": r.judge,
        }
        for r in rows
    ]
    statement = insert(vsrf_act).values(values)
    keep = (
        "number",
        "claim_id",
        "act_kind",
        "act_date",
        "instance",
        "subject",
        "collegium",
        "judge",
    )
    with engine.begin() as connection:
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["pdf_id"], set_={k: statement.excluded[k] for k in keep}
            )
        )


def _store_claims(engine: Engine, case_type: str, rows: list[ClaimRow]) -> None:
    rows = [row for row in rows if not row.economic]
    if not rows:
        return
    values = [
        {
            "claim_id": r.claim_id,
            "number": r.number,
            "received_date": r.received_date,
            "case_type": case_type,
            "instance": r.instance,
            "case_uid": r.case_uid,
            "first_court": r.first_court,
            "first_case_number": r.first_case_number,
            "subject": r.subject,
        }
        for r in rows
    ]
    statement = insert(vsrf_claim).values(values)
    keep = (
        "number",
        "received_date",
        "instance",
        "case_uid",
        "first_court",
        "first_case_number",
        "subject",
    )
    with engine.begin() as connection:
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["claim_id"], set_={k: statement.excluded[k] for k in keep}
            )
        )


SOURCES = {
    # Ключ различает пространства PDF: у экономической коллегии номера свои.
    "acts": (acts_url, parse_acts, _store_acts, lambda r: (r.economic, r.pdf_id)),
    "claims": (claims_url, parse_claims, _store_claims, lambda r: r.claim_id),
}


def sweep_listing(
    source: str,
    client,
    engine: Engine,
    store: RawStore,
    *,
    start: date = START,
    today: date | None = None,
    until: float | None = None,
) -> int:
    """Пройти окна выдачи по месяцам. Окно закрывается, только если
    собранное сошлось со счётчиком сайта: молчаливый недосбор здесь
    ловится так же, как у КСОЮ, — сверкой, а не аккуратностью."""
    build_url, parse, save, key = SOURCES[source]
    today = today or date.today()
    fresh = today - timedelta(days=RECHECK_DAYS)
    collected_total = 0

    for case_type in CASE_TYPES:
        with engine.connect() as connection:
            done = set(
                connection.execute(
                    select(vsrf_window.c.window_from).where(
                        vsrf_window.c.source == source, vsrf_window.c.case_type == case_type
                    )
                ).scalars()
            )
        for window_from, window_to in months(start, today):
            if window_from in done and window_to < fresh:
                continue
            if until is not None and time.monotonic() > until:
                return collected_total

            rows: dict = {}
            total = None
            page = 0
            while True:
                response = client.get(build_url(case_type, window_from, window_to, page))
                _save_raw(engine, store, response, "vsrf_listing")
                listing = listing_page(response.text)
                total = listing.total
                parsed = parse(listing.content)
                rows.update({key(row): row for row in parsed})
                # Кончилась выдача — по признаку сайта или по пустой странице.
                # Не по «нет наших строк»: страница экономической коллегии
                # своих строк не даёт, а за ней могут идти наши.
                if listing.last or not _items(listing.content):
                    break
                page += 1

            save(engine, case_type, list(rows.values()))
            collected_total += sum(
                1 for row in rows.values() if not getattr(row, "economic", False)
            )
            if total is not None and len(rows) == total:
                statement = insert(vsrf_window).values(
                    source=source,
                    case_type=case_type,
                    window_from=window_from,
                    window_to=window_to,
                    total=total,
                )
                with engine.begin() as connection:
                    connection.execute(
                        statement.on_conflict_do_update(
                            index_elements=["source", "case_type", "window_from"],
                            set_={
                                "window_to": statement.excluded.window_to,
                                "total": statement.excluded.total,
                                "done_at": func.now(),
                            },
                        )
                    )
            else:
                log.warning(
                    "%s %s %s: собрано %d, сайт обещал %s — окно не закрыто",
                    source,
                    case_type,
                    window_from,
                    len(rows),
                    total,
                )
    return collected_total


def fetch_texts(
    client, engine: Engine, store: RawStore, *, until: float | None = None, chunk: int = 200
) -> int:
    """Тексты актов из PDF, от свежего к старому. Очередь — `text_fetched_at
    IS NULL`, поэтому прерванный прогон продолжается сам."""
    fetched = 0
    while True:
        with engine.connect() as connection:
            pending = (
                connection.execute(
                    select(vsrf_act.c.pdf_id)
                    .where(vsrf_act.c.text_fetched_at.is_(None))
                    .order_by(vsrf_act.c.act_date.desc().nulls_last(), vsrf_act.c.pdf_id.desc())
                    .limit(chunk)
                )
                .scalars()
                .all()
            )
        if not pending:
            return fetched
        for pdf_id in pending:
            if until is not None and time.monotonic() > until:
                return fetched
            response = client.get(pdf_url(pdf_id))
            if not response.content.startswith(b"%PDF"):
                # Не PDF — не записываем ни текст, ни отметку: возьмём снова.
                log.warning("%s: вместо PDF пришло %d байт", pdf_id, len(response.content))
                continue
            raw_id = _save_raw(engine, store, response, "vsrf_act_pdf")
            try:
                text = pdf_text(response.content)
            except Exception as exc:  # noqa: BLE001 — один битый PDF не роняет сбор
                log.warning("%s: текст не вынут: %s", pdf_id, exc)
                text = ""
            statement = insert(vsrf_act_text).values(
                pdf_id=pdf_id, raw_page_id=raw_id, plain_text=text
            )
            with engine.begin() as connection:
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["pdf_id"],
                        set_={
                            "plain_text": statement.excluded.plain_text,
                            "raw_page_id": statement.excluded.raw_page_id,
                        },
                    )
                )
                connection.execute(
                    update(vsrf_act)
                    .where(vsrf_act.c.pdf_id == pdf_id)
                    .values(text_fetched_at=datetime.now().astimezone())
                )
            fetched += 1


def sweep(
    *,
    settings: Settings | None = None,
    only: str | None = None,
    start: date = START,
    max_hours: float = MAX_RUN_HOURS,
) -> dict[str, int]:
    """Один прогон: выдача актов, выдача «дел», затем тексты — до срока.

    Выдачи дешёвые (20 строк на запрос) и после первого прохода сводятся
    к перепроверке свежих месяцев; всё оставшееся время уходит на PDF.
    """
    from sqlalchemy import create_engine

    from .http import CourtClient, CourtOnCooldown, DailyCapReached

    settings = settings or default_settings
    engine = create_engine(settings.database_url)
    store = RawStore(settings.raw_root)
    until = time.monotonic() + max_hours * 3600
    result = {"acts": 0, "claims": 0, "texts": 0}

    with CourtClient(settings, bulk=True) as client:
        try:
            for source in ("acts", "claims"):
                if only in (None, source):
                    result[source] = sweep_listing(
                        source, client, engine, store, start=start, until=until
                    )
            if only in (None, "texts"):
                result["texts"] = fetch_texts(client, engine, store, until=until)
        except CourtOnCooldown as exc:
            # Сайт попросил отойти. Прогон кончается чисто, следующий поднимет таймер.
            log.warning("ВС придержал: %s", exc)
        except DailyCapReached as exc:
            # Предохранитель 20 000 запросов в сутки на хост — штатный конец дня,
            # а не поломка: иначе тревога писала бы о нём каждые сутки.
            log.info("%s", exc)
    engine.dispose()
    return result
