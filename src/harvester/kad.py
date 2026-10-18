"""КАД: дела СИП и арбитражных судов округов, их итоговые акты (docs/kad.md).

Три этапа, как у ВС:

1. **поиск** — окна по дате регистрации дела, сверка со счётчиком.
   Выдача отдаёт не больше 40 страниц по 25 дел, поэтому окно, где дел
   больше тысячи, делится пополам. Дело попадает в выдачу суда, когда
   у него появилась инстанция этого суда, — даже давно зарегистрированное,
   поэтому закрытые окна перепроверяются по счётчику (`RECOUNT_DAYS`);
2. **документы** — карточка дела и список документов инстанций наших судов.
   Реквизиты храним у всех актов, `wanted` — у тех, чей текст нужен;
3. **тексты** — PDF только у `wanted`, только с `TEXTS_FROM`.

Доступ (решение владельца, `docs/ethics.md`): cookie проверки браузера
получает Chromium с маскировкой автоматизации; поиск, карточки и списки
документов — обычным `CourtClient` с этими cookie; PDF — тем же браузером,
потому что у каждого файла своя проверка, решаемая скриптом страницы.
Cookie `wasm` живёт около 10 минут — сессия обновляет их сама.
Отказ (429, 451, страница придержания) сперва пробует другой адрес выхода
(`source_addresses`, решение владельца в `docs/ethics.md`); адреса
кончились — отступление, а на 451 со свежими cookie ещё и один перезапуск
браузера под новый отпечаток; не помогло — прогон кончается.

Маскировка (решение владельца 18.10.2026) по мотивам `cookie-fetcher`
проекта court-case-monitor (zeotq, CC BY-NC 4.0 — см. THIRD_PARTY_NOTICES.md):
флаги браузера, скрипт поверх страницы, подменяющий `navigator.webdriver`
и рвущий разведочный WebSocket, подавление трекеров и человеческая
последовательность «мышь → пауза → клик».
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from selectolax.parser import HTMLParser
from sqlalchemy import Engine, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .config import Settings
from .config import settings as default_settings
from .db.schema import kad_case, kad_doc, kad_doc_text, kad_window
from .db.store import save_raw_page
from .raw import RawStore

log = logging.getLogger("harvester.kad")

BASE = "https://kad.arbitr.ru"
HOST = "kad.arbitr.ru"
#: Суд по интеллектуальным правам и десять арбитражных судов округов.
COURTS = (
    "SIP",
    "FASVVO",
    "FASVSO",
    "FASDVO",
    "FASZSO",
    "FASMO",
    "FASPO",
    "FASSZO",
    "FASSKO",
    "FASUO",
    "FASCO",
)
#: Окна поиска — с этой даты регистрации. Дела 2023–2024 годов доходят
#: до кассации в 2025–2026, а тексты нужны с `TEXTS_FROM`.
START = date(2023, 1, 1)
#: С какой даты публикации качать тексты.
TEXTS_FROM = date(2025, 1, 1)
#: Выдача отдаёт не больше 40 страниц по 25 дел (замер 18.10.2026).
SEARCH_CAP = 1000
#: Как часто перепроверять счётчик закрытого окна: дела старой регистрации
#: попадают в выдачу суда, когда доходят до него.
RECOUNT_DAYS = 7
#: Как часто перечитывать документы дела, пока у нашей инстанции нет итога.
REFETCH_DAYS = 14
#: Пауза между запросами к КАД. 18.10.2026 при трёх секундах и
#: полутора сотнях запросов за полчаса КАД ответил 429.
PAUSE_SECONDS = 6.0
COOKIE_TTL = 8 * 60
CHROMIUM = "/usr/bin/chromium"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)
MAX_RUN_HOURS = 6.0

#: Трекеры: проверке не нужны, а свой след на сторонних доменах оставляют.
#: Список — как у court-case-monitor: mail.ru, аналитика Google и Яндекса,
#: vk.com, двойной клик.
BLOCKED = (
    "mail.ru",
    "yandex",
    "vk.com",
    "google-analytics",
    "googletagmanager",
    "doubleclick.net",
)

#: Поверх страницы: скрыть признак автоматизации и разведочный WebSocket.
#: `--disable-blink-features=AutomationControlled` снимает штатный признак,
#: но не мешает проверке прочитать его другими способами; defineProperty
#: перекрывает и их. WebSocket с адресом проверки («itsgonnafail») в
#: court-case-monitor подменён заглушкой «соединение закрыто» — проверка
#: считает отказ нормой и выдаёт cookie, настоящие сокеты идут как есть.
_STEALTH = """
(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => false });

    const NativeWS = window.WebSocket;
    function FakeWS(url, protocols) {
        if (String(url).includes("itsgonnafail")) {
            this.readyState = 3;
            this.CONNECTING = 0;
            this.OPEN = 1;
            this.CLOSING = 2;
            this.CLOSED = 3;
            this.send = () => {};
            this.close = () => {};
            return;
        }
        return new NativeWS(url, protocols);
    }
    FakeWS.prototype = NativeWS.prototype;
    window.WebSocket = FakeWS;
})();
"""

#: Определения, чей текст нужен наравне с итоговыми актами.
USEFUL = ("прекращении производства", "судебных расходов", "обеспечительн")


def wanted(type_name: str | None, content: str | None, file_name: str | None) -> bool:
    """Качать ли текст акта. Итоговые — «Решения и постановления»;
    из определений — короткий список. Резолютивная часть — нет: полный
    текст придёт отдельным актом. Без файла — нечего качать."""
    if not file_name:
        return False
    content = (content or "").lower()
    if "резолютивная часть" in content:
        return False
    return type_name == "Решения и постановления" or any(word in content for word in USEFUL)


# --- разбор ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseRow:
    case_id: str
    number: str
    case_kind: str | None
    registration_date: date | None
    judge: str | None


_CARD = re.compile(r"/Card/([0-9a-f-]{36})")


def parse_search(html: str) -> tuple[list[CaseRow], int | None]:
    """Строки выдачи и счётчик «всего». Стороны не берём: ФИО с ИНН."""
    tree = HTMLParser(html)
    total = tree.css_first("#documentsTotalCount")
    rows = []
    for tr in tree.css("tr"):
        link = tr.css_first("a.num_case")
        if link is None:
            continue
        found = _CARD.search(link.attributes.get("href") or "")
        if not found:
            continue
        kind = tr.css_first("td.num div[title]")
        judge = tr.css_first("td.court div.judge")
        day = re.match(
            r"(\d{2})\.(\d{2})\.(\d{4})", (kind.attributes.get("title") or "") if kind else ""
        )
        rows.append(
            CaseRow(
                case_id=found.group(1),
                number=" ".join(link.text().split()),
                case_kind=((kind.attributes.get("class") or "").split() or [None])[0]
                if kind
                else None,
                registration_date=date(int(day[3]), int(day[2]), int(day[1])) if day else None,
                judge=(judge.attributes.get("title") or None) if judge else None,
            )
        )
    value = total.attributes.get("value") if total else None
    return rows, int(value) if value and value.isdigit() else None


def _ms_date(value: str | None) -> datetime | None:
    """`/Date(1791456237000)/` → момент времени."""
    found = re.search(r"\d{10,}", value or "")
    return datetime.fromtimestamp(int(found.group()) / 1000, tz=UTC) if found else None


# --- доступ ----------------------------------------------------------------


class Blocked(RuntimeError):
    """КАД отказал после свежих cookie и перезапуска браузера: прогон кончается, а не долбит."""


def _blocked(status: int, text: str) -> bool:
    return status == 451 or "Доступ заблокирован" in text


class KadSession:
    """Браузер для cookie и PDF, `CourtClient` для остального."""

    def __init__(self, settings: Settings | None = None) -> None:
        import httpx
        from playwright.sync_api import sync_playwright

        from .http import CourtClient

        settings = (settings or default_settings).model_copy(
            update={"min_delay_seconds": PAUSE_SECONDS, "global_min_delay_seconds": PAUSE_SECONDS}
        )
        self._pw = sync_playwright().start()
        self._relaunched = False
        self._launch()
        #: Cookie проверки держим сами: при блокировке адреса клиент
        #: пересобирается под новый адрес выхода, а cookie это пережить
        #: обязаны — на них держится весь доступ к КАД.
        self._jar = httpx.Cookies()
        headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "X-Date-Format": "iso",
            "Origin": BASE,
            "Referer": BASE + "/",
        }

        def build(source: str | None) -> httpx.Client:
            client = httpx.Client(
                headers=headers,
                timeout=settings.request_timeout_seconds,
                follow_redirects=True,
                transport=httpx.HTTPTransport(local_address=source) if source else None,
            )
            for cookie in self._jar.jar:
                client.cookies.set(
                    cookie.name, cookie.value, domain=cookie.domain, path=cookie.path
                )
            return client

        self.client = CourtClient(settings, client_factory=build)
        self._refreshed = 0.0
        self.refresh()

    def _launch(self) -> None:
        """Поднять браузер с маскировкой.

        Флаги и скрипты — по court-case-monitor (THIRD_PARTY_NOTICES.md):
        их набор проходит вторую проверку КАД, честный headless — нет
        (docs/kad.md). Версия User-Agent подобрана под установленный
        Chromium: расходящиеся версия агента и движка — сам по себе
        отпечаток, поэтому чужой Chrome/120 не берём.
        """
        self._browser = self._pw.chromium.launch(
            executable_path=CHROMIUM,
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                # Программный WebGL — решение владельца (docs/ethics.md):
                # без него проверка не проходит, рендеринг она не считает.
                "--enable-unsafe-swiftshader",
                "--enable-features=WebAssembly",
                "--enable-webgl",
                "--enable-accelerated-2d-canvas",
                "--font-render-hinting=none",
                "--no-first-run",
                "--hide-scrollbars",
                "--mute-audio",
                "--window-size=1366,768",
            ],
        )
        self._ctx = self._browser.new_context(
            user_agent=UA,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={"Wasm-Support": "true"},
        )
        self._ctx.add_init_script(_STEALTH)
        # Трекеры — первым слоем, PDF перехват вторым: последний перехват
        # в Playwright проверяется раньше, и вёрстка проверки доходит до
        # своих доменов через continue_, а наши PDF — через fetch.
        self._ctx.route("**/*", self._route)
        self._pdf: bytes | None = None
        self._ctx.route("**/Document/Pdf/**", self._capture_pdf)
        self._page = self._ctx.new_page()

    def _route(self, route) -> None:
        """Трекеры оборвать, остальное пропустить как есть."""
        url = route.request.url
        if any(domain in url for domain in BLOCKED):
            route.abort()
        else:
            route.continue_()

    def _capture_pdf(self, route) -> None:
        response = route.fetch()
        body = response.body()
        if body[:4] == b"%PDF":
            self._pdf = body
        route.fulfill(response=response)

    def _count(self) -> None:
        """Шаг браузера — тоже запрос к КАД: та же пауза и тот же счётчик."""
        self.client._throttle(HOST)
        from .http import _today

        key = (HOST, _today())
        self.client._requests_today[key] = self.client._requests_today.get(key, 0) + 1

    def refresh(self) -> None:
        """Cookie проверки: главная, затем поиск — его кнопка запускает
        вторую проверку (`wasm`). Номер дела любой, результат не нужен.

        Перед заполнением формы — движение мыши и пауза на кадр отрисовки:
        проверка смотрит не только на cookie, но и на то, как страницей
        управляли; мгновенный клик сразу после загрузки — признак скрипта.
        """
        self._count()
        self._page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        self._page.wait_for_timeout(8000)
        # Окна уведомления может не быть.
        with contextlib.suppress(Exception):
            self._page.click(".js-promo_notification-popup-close", timeout=3000)
        self._page.mouse.move(240, 180, steps=12)
        self._page.evaluate(
            """async () => {
                await new Promise(requestAnimationFrame);
                await new Promise(resolve => setTimeout(resolve, 250));
            }"""
        )
        self._page.fill("#sug-cases textarea, #sug-cases input", "СИП-1/2026")
        self._page.click("#b-form-submit button", delay=100)
        # Ожидание с растущей задержкой: cookie приходят не мгновенно, а
        # постоянный интервал опрашивает страницу ровнее человека.
        delay = 500
        for _ in range(20):
            if any(c["name"] == "wasm" for c in self._ctx.cookies(BASE)):
                break
            self._page.wait_for_timeout(delay)
            delay = min(int(delay * 1.5), 3000)
        else:
            raise Blocked("КАД не выдал cookie проверки браузера")
        self._jar.clear()
        for c in self._ctx.cookies(BASE):
            self._jar.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        self._refreshed = time.monotonic()
        log.info("КАД: cookie обновлены")

    def _relaunch(self) -> None:
        """Свежий браузер — новый отпечаток.

        КАД, задержавший запрос со свежими cookie, мог запомнить не только
        сессию, но и сам браузер. Один перезапуск за прогон дешевле, чем
        конец прогона на первой же 451.
        """
        log.info("КАД: браузер перезапускается под новый отпечаток")
        for close in (self._page.close, self._ctx.close, self._browser.close):
            with contextlib.suppress(Exception):
                close()
        self._relaunched = True
        self._launch()
        self.refresh()

    def _send(self, call):
        if time.monotonic() - self._refreshed > COOKIE_TTL:
            self.refresh()
        response = call()
        if _blocked(response.status_code, response.text):
            self.refresh()
            response = call()
            if _blocked(response.status_code, response.text):
                if not self._relaunched:
                    self._relaunch()
                    response = call()
                if _blocked(response.status_code, response.text):
                    raise Blocked(f"КАД: {response.status_code} и со свежими cookie")
        return response

    def search(self, court: str, start: date, end: date, page: int = 1):
        payload = {
            "Page": page,
            "Count": 25,
            "Courts": [court],
            "DateFrom": f"{start:%Y-%m-%d}T00:00:00",
            "DateTo": f"{end:%Y-%m-%d}T23:59:59",
            "Sides": [],
            "Judges": [],
            "CaseNumbers": [],
            "WithVKSInstances": False,
        }
        return self._send(lambda: self.client.post(f"{BASE}/Kad/SearchInstances", json=payload))

    def card(self, case_id: str):
        return self._send(lambda: self.client.get(f"{BASE}/Card/{case_id}"))

    def docs(self, instance_id: str, case_id: str, page: int = 1):
        return self._send(
            lambda: self.client.get(
                f"{BASE}/Kad/InstanceDocumentsPage?id={instance_id}&caseId={case_id}"
                f"&withProtocols=true&perPage=30&page={page}"
            )
        )

    def pdf(self, case_id: str, doc_id: str, file_name: str) -> bytes | None:
        if time.monotonic() - self._refreshed > COOKIE_TTL:
            self.refresh()
        self._count()
        self._pdf = None
        try:
            self._page.goto(
                f"{BASE}/Document/Pdf/{case_id}/{doc_id}/{file_name}?isAddStamp=True",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception as exc:  # noqa: BLE001 — встроенный просмотрщик рвёт навигацию
            log.debug("%s: %s", doc_id, exc)
        for _ in range(30):
            if self._pdf is not None:
                break
            self._page.wait_for_timeout(500)
        return self._pdf

    def close(self) -> None:
        self.client.close()
        self._browser.close()
        self._pw.stop()


# --- поиск -----------------------------------------------------------------


def months(start: date, end: date) -> Iterator[tuple[date, date]]:
    current = date(start.year, start.month, 1)
    while current <= end:
        following = date(current.year + current.month // 12, current.month % 12 + 1, 1)
        yield max(current, start), min(following - timedelta(days=1), end)
        current = following


def _save_raw(engine: Engine, store: RawStore, url: str, content: bytes, kind: str) -> int:
    record = store.save(
        content,
        url=url,
        court_domain=HOST,
        http_status=200,
        content_kind=kind,
        extension="pdf" if kind == "kad_pdf" else "html",
    )
    with engine.begin() as connection:
        return save_raw_page(connection, record)


def _collect(session, engine, store, court, start, end) -> tuple[dict[str, CaseRow], int | None]:
    rows: dict[str, CaseRow] = {}
    response = session.search(court, start, end)
    found, total = parse_search(response.text)
    _save_raw(engine, store, response.url, response.content, "kad_search")
    if total is None or total > SEARCH_CAP:
        return {}, total
    rows.update({r.case_id: r for r in found})
    for page in range(2, -(-total // 25) + 1):
        response = session.search(court, start, end, page)
        _save_raw(engine, store, response.url, response.content, "kad_search")
        rows.update({r.case_id: r for r in parse_search(response.text)[0]})
    return rows, total


def _closed(session, engine, store, court, start, end) -> tuple[dict[str, CaseRow], int, bool]:
    """Окно, сверенное со счётчиком. Больше тысячи дел или не сошлось — пополам."""
    rows, total = _collect(session, engine, store, court, start, end)
    if total is not None and total <= SEARCH_CAP and len(rows) == total:
        return rows, total, True
    if start >= end:
        return rows, total or len(rows), False
    middle = start + (end - start) // 2
    left, lt, lok = _closed(session, engine, store, court, start, middle)
    right, rt, rok = _closed(session, engine, store, court, middle + timedelta(days=1), end)
    return {**left, **right}, lt + rt, lok and rok


def _store_cases(engine: Engine, court: str, rows: list[CaseRow]) -> None:
    if not rows:
        return
    statement = insert(kad_case).values(
        [
            {
                "case_id": r.case_id,
                "number": r.number,
                "court_tag": court,
                "case_kind": r.case_kind,
                "registration_date": r.registration_date,
                "judge": r.judge,
            }
            for r in rows
        ]
    )
    with engine.begin() as connection:
        connection.execute(statement.on_conflict_do_nothing(index_elements=["case_id"]))


def sweep_search(
    session,
    engine: Engine,
    store: RawStore,
    *,
    start: date = START,
    today: date | None = None,
    until: float | None = None,
) -> int:
    """Окна по месяцам регистрации. Закрытое окно перепроверяется раз
    в `RECOUNT_DAYS` одним запросом счётчика: вырос — собирается заново."""
    today = today or date.today()
    stale = datetime.now(UTC) - timedelta(days=RECOUNT_DAYS)
    stored = 0
    for court in COURTS:
        with engine.connect() as connection:
            done = {
                row.window_from: row
                for row in connection.execute(
                    select(kad_window).where(kad_window.c.court_tag == court)
                )
            }
        for start_day, end_day in months(start, today):
            if until is not None and time.monotonic() > until:
                return stored
            known = done.get(start_day)
            if known is not None and known.window_to == end_day:
                if known.done_at > stale:
                    continue
                _, total = parse_search(session.search(court, start_day, end_day).text)
                if total == known.total:
                    with engine.begin() as connection:
                        connection.execute(
                            update(kad_window)
                            .where(
                                kad_window.c.court_tag == court,
                                kad_window.c.window_from == start_day,
                            )
                            .values(done_at=func.now())
                        )
                    continue
            rows, total, ok = _closed(session, engine, store, court, start_day, end_day)
            _store_cases(engine, court, list(rows.values()))
            stored += len(rows)
            log.info(
                "%s %s—%s: %d дел из %s%s",
                court,
                start_day,
                end_day,
                len(rows),
                total,
                "" if ok else " — окно не закрыто",
            )
            if ok:
                statement = insert(kad_window).values(
                    court_tag=court, window_from=start_day, window_to=end_day, total=total
                )
                with engine.begin() as connection:
                    connection.execute(
                        statement.on_conflict_do_update(
                            index_elements=["court_tag", "window_from"],
                            set_={"window_to": end_day, "total": total, "done_at": func.now()},
                        )
                    )
    return stored


# --- документы -------------------------------------------------------------


def _store_docs(engine: Engine, case_id: str, court: str, instance: dict, items: list[dict]) -> int:
    rows = []
    for d in items:
        if not d.get("IsAct") or d.get("IsDeleted"):
            continue
        content = "; ".join(d.get("ContentTypes") or []) or None
        rows.append(
            {
                "doc_id": d["Id"],
                "case_id": case_id,
                "instance_id": instance["Id"],
                "court_tag": court,
                "instance_level": instance.get("InstanceLevel"),
                "type_name": d.get("DocumentTypeName"),
                "content": content,
                "finish_instance": bool(d.get("FinishInstance")),
                "decision": d.get("DecisionTypeName"),
                "publish_date": _ms_date(d.get("PublishDate")),
                "file_name": d.get("FileName") or None,
                "wanted": wanted(d.get("DocumentTypeName"), content, d.get("FileName")),
            }
        )
    if rows:
        statement = insert(kad_doc).values(rows)
        keep = (
            "type_name",
            "content",
            "finish_instance",
            "decision",
            "publish_date",
            "file_name",
            "wanted",
        )
        with engine.begin() as connection:
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=["doc_id"], set_={k: statement.excluded[k] for k in keep}
                )
            )
    return len(rows)


def fetch_docs(session, engine: Engine, *, until: float | None = None, chunk: int = 200) -> int:
    """Документы дел: впервые — у всех, затем раз в `REFETCH_DAYS`, пока
    у инстанции нашего суда нет акта, которым она закончилась."""
    import json

    stale = datetime.now(UTC) - timedelta(days=REFETCH_DAYS)
    finished = (
        select(kad_doc.c.case_id)
        .where(kad_doc.c.case_id == kad_case.c.case_id, kad_doc.c.finish_instance)
        .exists()
    )
    stored = 0
    while True:
        with engine.connect() as connection:
            pending = connection.execute(
                select(kad_case.c.case_id, kad_case.c.court_tag)
                .where(
                    or_(
                        kad_case.c.docs_fetched_at.is_(None),
                        and_(kad_case.c.docs_fetched_at < stale, ~finished),
                    )
                )
                .order_by(
                    kad_case.c.docs_fetched_at.nulls_first(), kad_case.c.registration_date.desc()
                )
                .limit(chunk)
            ).all()
        if not pending:
            return stored
        for row in pending:
            if until is not None and time.monotonic() > until:
                return stored
            info = json.loads(session.card(row.case_id).content)["Result"]["CaseInfo"]
            for instance in info.get("Instances") or []:
                court = (instance.get("Court") or {}).get("Tag")
                if court not in COURTS:
                    continue
                page = 1
                while True:
                    result = json.loads(session.docs(instance["Id"], row.case_id, page).content)[
                        "Result"
                    ]
                    stored += _store_docs(
                        engine, row.case_id, court, instance, result.get("Items") or []
                    )
                    if page >= (result.get("PagesCount") or 1):
                        break
                    page += 1
            with engine.begin() as connection:
                connection.execute(
                    update(kad_case)
                    .where(kad_case.c.case_id == row.case_id)
                    .values(docs_fetched_at=func.now())
                )


# --- тексты ----------------------------------------------------------------


def fetch_texts(session, engine: Engine, store: RawStore, *, until: float | None = None) -> int:
    """PDF нужных актов с `TEXTS_FROM`, от свежих к старым. Не отдался
    файл — акт отмечается взятым без текста, чтобы не вставать на нём."""
    from .vsrf import pdf_text

    fetched = 0
    while True:
        with engine.connect() as connection:
            pending = connection.execute(
                select(kad_doc.c.doc_id, kad_doc.c.case_id, kad_doc.c.file_name)
                .where(
                    kad_doc.c.wanted,
                    kad_doc.c.text_fetched_at.is_(None),
                    kad_doc.c.publish_date
                    >= datetime.combine(TEXTS_FROM, datetime.min.time(), UTC),
                )
                .order_by(kad_doc.c.publish_date.desc())
                .limit(100)
            ).all()
        if not pending:
            return fetched
        for row in pending:
            if until is not None and time.monotonic() > until:
                return fetched
            body = session.pdf(row.case_id, row.doc_id, row.file_name)
            with engine.begin() as connection:
                connection.execute(
                    update(kad_doc)
                    .where(kad_doc.c.doc_id == row.doc_id)
                    .values(text_fetched_at=func.now())
                )
            if body is None:
                log.warning("%s: PDF не отдан", row.doc_id)
                continue
            url = f"{BASE}/Document/Pdf/{row.case_id}/{row.doc_id}/{row.file_name}"
            raw_id = _save_raw(engine, store, url, body, "kad_pdf")
            try:
                text = pdf_text(body, layout=True)
            except Exception as exc:  # noqa: BLE001 — один битый PDF не роняет сбор
                log.warning("%s: текст не вынут: %s", row.doc_id, exc)
                text = ""
            statement = insert(kad_doc_text).values(
                doc_id=row.doc_id, raw_page_id=raw_id, plain_text=text
            )
            with engine.begin() as connection:
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["doc_id"],
                        set_={
                            "plain_text": statement.excluded.plain_text,
                            "raw_page_id": statement.excluded.raw_page_id,
                        },
                    )
                )
            fetched += 1


def sweep(
    *, settings: Settings | None = None, only: str | None = None, max_hours: float = MAX_RUN_HOURS
) -> dict[str, int]:
    from sqlalchemy import create_engine

    from .http import CourtOnCooldown, DailyCapReached

    settings = settings or default_settings
    engine = create_engine(settings.database_url)
    store = RawStore(settings.raw_root)
    until = time.monotonic() + max_hours * 3600
    result = {"search": 0, "docs": 0, "texts": 0}
    session = KadSession(settings)
    try:
        if only in (None, "search"):
            result["search"] = sweep_search(session, engine, store, until=until)
        if only in (None, "docs"):
            result["docs"] = fetch_docs(session, engine, until=until)
        if only in (None, "texts"):
            result["texts"] = fetch_texts(session, engine, store, until=until)
    except CourtOnCooldown as exc:
        log.warning("КАД придержал: %s", exc)
    except DailyCapReached as exc:
        log.info("%s", exc)
    finally:
        session.close()
        engine.dispose()
    return result
