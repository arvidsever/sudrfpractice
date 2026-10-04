"""КС РФ: решения Конституционного Суда (docs/ksrf.md).

Сайт на «Битриксе»: список решений `/Decision/` по 10 на страницу, листание
параметром `PAGEN_1`, каждое решение — PDF с текстовым слоем. Общего
счётчика нет, есть только число страниц.

Две особенности доступа:

* сайт не пускает адреса сервера; ходим через обратный туннель с мака
  владельца — `ksrf.ru` на сервере направлен в `/etc/hosts` на `127.0.0.1`.
  Туннеля нет — соединение отказано, и прогон кончается «не сейчас»,
  а не падением: дособирать новое можно, когда туннель снова поднят;
* сертификат на корне Минцифры, которого нет в стандартных хранилищах.
  Корень лежит рядом (`certs/`) и подключается только для КС.
"""

from __future__ import annotations

import logging
import re
import ssl
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from selectolax.parser import HTMLParser
from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert

from .config import Settings
from .config import settings as default_settings
from .db.schema import ksrf_decision, ksrf_decision_text
from .db.store import save_raw_page
from .raw import RawStore
from .vsrf import pdf_text

log = logging.getLogger("harvester.ksrf")

BASE = "https://www.ksrf.ru"
DOMAIN = "www.ksrf.ru"
ROOT_CA = Path(__file__).parent / "certs" / "russian_trusted_root_ca.pem"
MAX_RUN_HOURS = 6.0

_PDF = re.compile(r"/doc/KSRFDecision(\d+)\.pdf")
_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_KIND = re.compile(r"-([А-ЯЁ]+)/")


@dataclass(frozen=True, slots=True)
class Decision:
    pdf_id: int
    number: str | None
    kind: str | None
    decision_date: date | None
    title: str | None


def parse_list(html: str) -> tuple[list[Decision], int | None]:
    """Строки страницы и номер последней страницы из листалки."""
    tree = HTMLParser(html)
    rows = []
    for item in tree.css("ol.list-hover li"):
        anchor = next(
            (a for a in item.css("a[href]") if _PDF.search(a.attributes.get("href") or "")), None
        )
        if anchor is None:
            continue
        spans = [" ".join(s.text().split()) for s in item.css("span")]
        number = " ".join(anchor.text().split()) or None
        found = next((_DATE.search(s) for s in spans if _DATE.search(s)), None)
        kind = _KIND.search(number or "")
        rows.append(
            Decision(
                pdf_id=int(_PDF.search(anchor.attributes["href"]).group(1)),
                number=number,
                kind=kind.group(1) if kind else None,
                decision_date=(
                    date(int(found.group(3)), int(found.group(2)), int(found.group(1)))
                    if found
                    else None
                ),
                title=max(spans, key=len) if spans else None,
            )
        )
    pages = [int(x) for x in re.findall(r"PAGEN_1=(\d+)", html)]
    return rows, max(pages) if pages else None


def list_url(page: int) -> str:
    return f"{BASE}/Decision/" + (f"?PAGEN_1={page}" if page > 1 else "")


def pdf_url(pdf_id: int) -> str:
    return f"{BASE}/doc/KSRFDecision{pdf_id}.pdf"


def _client(settings: Settings):
    import certifi
    import httpx

    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(ROOT_CA))
    return httpx.Client(
        verify=context,
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent, "Accept-Language": "ru,en;q=0.8"},
    )


def _save_raw(engine: Engine, store: RawStore, response, kind: str) -> int:
    record = store.save(
        response.content,
        url=str(response.url),
        court_domain=DOMAIN,
        http_status=response.status_code,
        content_kind=kind,
        extension="pdf" if kind == "ksrf_pdf" else "html",
    )
    with engine.begin() as connection:
        return save_raw_page(connection, record)


def _store(engine: Engine, rows: list[Decision]) -> int:
    rows = list({row.pdf_id: row for row in rows}.values())
    if not rows:
        return 0
    statement = insert(ksrf_decision).values(
        [
            {
                "pdf_id": r.pdf_id,
                "number": r.number,
                "kind": r.kind,
                "decision_date": r.decision_date,
                "title": r.title,
            }
            for r in rows
        ]
    )
    keep = ("number", "kind", "decision_date", "title")
    with engine.begin() as connection:
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["pdf_id"], set_={k: statement.excluded[k] for k in keep}
            )
        )
    return len(rows)


def _known(engine: Engine, ids: list[int]) -> set[int]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                select(ksrf_decision.c.pdf_id).where(ksrf_decision.c.pdf_id.in_(ids))
            ).scalars()
        )


def sweep_list(client, engine: Engine, store: RawStore, *, until: float | None = None) -> int:
    """Пройти список от новых к старым.

    Первый проход — весь список, около 6 000 страниц: решений в базе меньше,
    чем страниц на сайте. Новые решения встают в начало и сдвигают
    остальные вниз, так что при проходе от новых к старым строки только
    повторяются, но не теряются. Дальше — дособор: страницы с начала
    до первой, где все решения уже известны.
    """
    response = client.get(list_url(1))
    _save_raw(engine, store, response, "ksrf_list")
    rows, last = parse_list(response.text)
    with engine.connect() as connection:
        have = connection.execute(select(func.count()).select_from(ksrf_decision)).scalar_one()
    backfill = last is not None and have < (last - 1) * 10
    log.info(
        "КС: страниц %s, решений в базе %d — %s",
        last,
        have,
        "весь список" if backfill else "дособор",
    )

    def take(rows: list[Decision]) -> int:
        """Записать страницу; вернуть, сколько на ней было новых. Новизну
        считаем ДО записи — после неё новых не бывает."""
        new = {r.pdf_id for r in rows} - _known(engine, [r.pdf_id for r in rows])
        _store(engine, rows)
        return len(new)

    seen = take(rows)
    page = 1
    while last is None or page < last:
        if until is not None and time.monotonic() > until:
            break
        page += 1
        response = client.get(list_url(page))
        _save_raw(engine, store, response, "ksrf_list")
        rows, more = parse_list(response.text)
        if not rows:
            break
        last = max(last or 0, more or 0)
        new = take(rows)
        seen += new
        if not backfill and new == 0:
            # Дособор: страница целиком знакомая — дальше только старое.
            break
    return seen


def fetch_texts(client, engine: Engine, store: RawStore, *, until: float | None = None) -> int:
    """Тексты из PDF, от новых к старым. Очередь — `text_fetched_at IS NULL`."""
    fetched = 0
    while True:
        with engine.connect() as connection:
            pending = (
                connection.execute(
                    select(ksrf_decision.c.pdf_id)
                    .where(ksrf_decision.c.text_fetched_at.is_(None))
                    .order_by(ksrf_decision.c.decision_date.desc().nulls_last())
                    .limit(200)
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
                log.warning("%s: вместо PDF пришло %d байт", pdf_id, len(response.content))
                continue
            raw_id = _save_raw(engine, store, response, "ksrf_pdf")
            try:
                text = pdf_text(response.content)
            except Exception as exc:  # noqa: BLE001 — один битый PDF не роняет сбор
                log.warning("%s: текст не вынут: %s", pdf_id, exc)
                text = ""
            statement = insert(ksrf_decision_text).values(
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
                    update(ksrf_decision)
                    .where(ksrf_decision.c.pdf_id == pdf_id)
                    .values(text_fetched_at=datetime.now().astimezone())
                )
            fetched += 1


class Unreachable(RuntimeError):
    """Сайт КС недоступен — туннеля нет. «Не сейчас», а не поломка."""


def sweep(
    *, settings: Settings | None = None, only: str | None = None, max_hours: float = MAX_RUN_HOURS
) -> dict[str, int]:
    from sqlalchemy import create_engine

    from .http import CourtClient, CourtOnCooldown, DailyCapReached

    settings = settings or default_settings
    engine = create_engine(settings.database_url)
    store = RawStore(settings.raw_root)
    until = time.monotonic() + max_hours * 3600
    result = {"list": 0, "texts": 0}
    with CourtClient(settings, bulk=True, client=_client(settings)) as client:
        try:
            if only in (None, "list"):
                result["list"] = sweep_list(client, engine, store, until=until)
            if only in (None, "texts"):
                result["texts"] = fetch_texts(client, engine, store, until=until)
        except CourtOnCooldown as exc:
            log.warning("КС придержал: %s", exc)
        except DailyCapReached as exc:
            log.info("%s", exc)
        except RuntimeError as exc:
            # Клиент исчерпал повторы на соединении: туннеля с мака нет.
            if "не удалось получить ответ" in str(exc):
                raise Unreachable(str(exc)) from exc
            raise
        finally:
            engine.dispose()
    return result
