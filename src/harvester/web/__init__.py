"""Веб-интерфейс поиска: read-only API и одна страница (roadmap 7.1).

Намеренные упрощения:

* **API ничего не придумывает, а зовёт `search`.** Те же `Query`, `run`,
  `run_higher`, что и у CLI и MCP: второго, расходящегося поиска не появляется;
* **наружу не открыт.** Слушает `127.0.0.1`, к нему ходят через `ssh -L`
  (docs/web.md). Корпус содержит необезличенные ФИО, и публикация —
  отдельное решение владельца, а не побочный эффект этого модуля;
* **только чтение — на уровне соединения**, а не обещанием кода: в сессии
  `default_transaction_read_only=on` и потолок в 30 с;
* **страница одна и без сборки**: `index.html` с ванильным JS. Фреймворк
  и сборщик здесь были бы нагрузкой больше самой страницы.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi import Query as Param
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.exc import DBAPIError

from ..config import settings
from ..db.schema import act, act_text, case, court
from ..norms import split_query
from ..search import (
    HIGHER,
    Query,
    instances,
    run,
    run_higher,
    uid_of_supreme_act,
    wants_higher,
)

#: «Найдено» считается до этого числа и дальше пишется «более».
COUNT_CAP = 10_000

from . import appstore  # noqa: E402
from . import auth as authlib  # noqa: E402
from . import tokens as tokenlib  # noqa: E402

PAGE = Path(__file__).with_name("index.html")

#: Суды общей юрисдикции по инстанциям — те же домены, что в докстринге
#: MCP-инструмента `search`: 1–9 кассационные, военный, 1–5 апелляционные.
INSTANCES = {
    "kas": tuple(f"{n}kas.sudrf.ru" for n in (*range(1, 10), "v")),
    "ap": tuple(f"{n}ap.sudrf.ru" for n in (*range(1, 6), "v")),
    "vs": ("www.vsrf.ru",),
    "ks": ("www.ksrf.ru",),
}

#: Сколько актов ВС и КС показывать над основной выдачей на первой странице.
HIGHER_TOP = 5

#: Номер дела в строке поиска: «88-17060/2026», «5-КГ26-1-К2», «57-П/2026».
# ponytail: эвристика по виду номера. Слово вроде «01-02-2024» она примет
# за номер; хватит, пока строка одна. Станет мешать — отдельное поле.
NUMBER = re.compile(r"(?<![\w-])\d{1,3}-[\w/-]*\d[\w/-]*(?![\w-])")


def _connect() -> Engine:
    return create_engine(
        settings.database_url,
        connect_args={"options": "-c default_transaction_read_only=on -c statement_timeout=30000"},
    )


def _norm_title(key: str) -> str:
    code, number = key.split(":", 1)
    if code == "Конституция":
        return f"ст. {number} Конституции РФ"
    return f"ст. {number} {code}" + ("" if code == "КоАП" else " РФ")


_FROM_ENV = object()

#: Браузеру нечего брать с чужих адресов: страница и API — одно происхождение.
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _tokens_from_env(environ=os.environ) -> tokenlib.Store | None:
    """Хранилище токенов приложения; без пути — токенов нет вовсе."""
    path = environ.get("SUDRF_WEB_TOKENS_PATH", "").strip()
    return tokenlib.Store(path) if path else None


def _registration_from_env(environ=os.environ) -> appstore.Policy | None:
    """Регистрация установок из TestFlight — только явным включением:
    она открывает корпус с ФИО каждому, кто поставил сборку по ссылке."""
    if environ.get("SUDRF_WEB_APP_REGISTRATION") != "1":
        return None
    apple_id = environ.get("SUDRF_WEB_APP_APPLE_ID", "").strip()
    return appstore.Policy(
        bundle_id=environ.get("SUDRF_WEB_APP_BUNDLE_ID", "ru.sudrf.app").strip(),
        environments=frozenset(
            e.strip()
            for e in environ.get("SUDRF_WEB_APP_ENVIRONMENTS", "Sandbox,Production").split(",")
            if e.strip()
        ),
        app_apple_id=int(apple_id) if apple_id else None,
    )


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "?"


def create_app(
    engine: Engine | None = None,
    auth: authlib.Auth | None | object = _FROM_ENV,
    tokens: tokenlib.Store | None | object = _FROM_ENV,
    registration: appstore.Policy | None | object = _FROM_ENV,
    limiter: tokenlib.Limiter | None = None,
) -> FastAPI:
    engine = engine or _connect()
    if auth is _FROM_ENV:
        auth = authlib.from_env()
    if tokens is _FROM_ENV:
        tokens = _tokens_from_env()
    if registration is _FROM_ENV:
        registration = _registration_from_env()
    limiter = limiter or tokenlib.Limiter()
    app = FastAPI(title="Судебная практика", docs_url=None, redoc_url=None)

    async def by_token(request: Request, call_next, token: str):
        """Приложение Sudrf: токен вместо cookie, со своим лимитом.
        Неверный токен идёт в тот же счёт неудач, что и неверный пароль."""
        client = _client_address(request)
        if auth.blocked(client):
            return JSONResponse({"detail": "Слишком много попыток. Подождите."}, status_code=429)
        found = await run_in_threadpool(tokens.check, token) if tokens else None
        if found is None:
            auth.failed(client)
            return JSONResponse({"detail": "Токен не принят."}, status_code=401)
        wait = limiter.acquire(found.id)
        if wait is not None:
            return JSONResponse(
                {"detail": "Слишком много запросов. Подождите немного."},
                status_code=429,
                headers={"Retry-After": str(wait)},
            )
        try:
            return await call_next(request)
        finally:
            limiter.release(found.id)

    @app.middleware("http")
    async def gate(request: Request, call_next):
        """Пароль до первой страницы: без cookie открыт только `/login`.
        Приложение Sudrf ходит в `/api/*` с токеном, регистрация открыта."""
        path = request.url.path
        bearer = request.headers.get("authorization", "")
        if auth and path.startswith("/api/") and bearer[:7].lower() == "bearer ":
            response = await by_token(request, call_next, bearer[7:].strip())
        elif (
            auth
            and path not in ("/login", "/logout", "/api/register")
            and not auth.valid_cookie(request.cookies.get(authlib.COOKIE))
        ):
            if path.startswith("/api/"):
                response = JSONResponse({"detail": "Нужен вход"}, status_code=401)
            else:
                response = RedirectResponse("/login", status_code=303)
        else:
            response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        # Слова поиска и акты не должны оседать в кэшах браузера и прокси.
        response.headers["Cache-Control"] = "no-store"
        if auth and auth.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def login_page(message: str = "", status: int = 200) -> HTMLResponse:
        note = f'<div class="err" role="alert">{message}</div>' if message else ""
        return HTMLResponse(authlib.LOGIN_PAGE.format(message=note), status_code=status)

    @app.get("/login", include_in_schema=False)
    def login_form(request: Request):
        if auth and auth.valid_cookie(request.cookies.get(authlib.COOKIE)):
            return RedirectResponse("/", status_code=303)
        return login_page()

    @app.post("/login", include_in_schema=False)
    async def login(request: Request):
        if not auth:
            return RedirectResponse("/", status_code=303)
        client = request.client.host if request.client else "?"
        if auth.blocked(client):
            return login_page("Слишком много попыток. Подождите четверть часа.", 429)
        body = parse_qs((await request.body()).decode("utf-8", "replace"), max_num_fields=4)
        password = (body.get("password") or [""])[0][:200]
        if not await run_in_threadpool(authlib.check_password, password, auth.password_hash):
            auth.failed(client)
            await asyncio.sleep(1)  # перебор по сети не должен быть быстрым
            return login_page("Неверный пароль.", 401)
        auth.forgive(client)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            authlib.COOKIE,
            auth.make_cookie(),
            max_age=authlib.SESSION_DAYS * 86400,
            httponly=True,
            secure=auth.secure_cookie,
            samesite="lax",
        )
        return response

    @app.get("/logout", include_in_schema=False)
    def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(authlib.COOKIE)
        return response

    @app.post("/api/register", include_in_schema=False)
    async def register(request: Request) -> JSONResponse:
        """Токен установки по подписанной Apple записи `AppTransaction`."""
        if registration is None or tokens is None:
            # 404, а не 403: приложение отличает «регистрации здесь нет» от
            # отказа записи и в этом случае прячет вкладку, а не показывает
            # тестеру ошибку. Так же отвечает и сервер без этого адреса.
            return JSONResponse(
                {"detail": "Регистрация установок на этом сервере выключена."}, status_code=404
            )
        client = _client_address(request)
        if auth and auth.blocked(client):
            return JSONResponse({"detail": "Слишком много попыток. Подождите."}, status_code=429)
        body = await request.body()
        try:
            if len(body) > appstore.MAX_JWS + 1024:
                raise ValueError
            jws = json.loads(body)["app_transaction"]
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"detail": "Нужно поле app_transaction."}, status_code=400)
        try:
            found = await run_in_threadpool(appstore.verify, jws, registration)
        except appstore.Rejected as exc:
            if auth:
                auth.failed(client)
            return JSONResponse({"detail": str(exc)}, status_code=403)
        if tokens.active_count(found.holder) >= tokenlib.MAX_PER_HOLDER:
            return JSONResponse(
                {
                    "detail": f"На этот Apple ID уже зарегистрировано {tokenlib.MAX_PER_HOLDER}"
                    " установки. Попросите владельца отозвать лишнюю."
                },
                status_code=409,
            )
        token = tokens.issue("install", found.holder, found.environment)
        return JSONResponse({"token": token})

    @app.get("/api/session")
    def session() -> dict:
        return {"auth": auth is not None}

    @app.exception_handler(DBAPIError)
    def database_failed(_, exc: DBAPIError) -> JSONResponse:
        # Потолок в 30 с — часть защиты (`_connect`), и упираются в него
        # настоящие запросы: «норма без слов» на живом корпусе идёт дольше.
        # Это ответ «слишком широко», а не поломка, и страница должна
        # сказать, что сузить.
        if "statement timeout" in str(exc):
            return JSONResponse(
                {
                    "detail": "Запрос не уложился в 30 секунд. Сузьте его: "
                    "добавьте слова, выберите инстанцию."
                },
                status_code=503,
            )
        return JSONResponse({"detail": "Корпус не ответил."}, status_code=502)

    titles: dict[str, str] = {}

    def court_title(domain: str) -> str:
        if not titles:
            with engine.connect() as connection:
                titles.update(connection.execute(select(court.c.domain, court.c.title)).all())
        return HIGHER[domain].title if domain in HIGHER else titles.get(domain, domain)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(PAGE, media_type="text/html; charset=utf-8")

    @app.get("/api/search")
    def search(
        q: str = "",
        instance: str = "all",
        sort: str = "relevance",
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = Param(25, ge=1, le=50),
        offset: int = Param(0, ge=0),
    ) -> dict:
        norms, rest = split_query(q)
        number = None
        if found := NUMBER.search(rest):
            number = found.group()
            rest = " ".join(rest.replace(number, " ").split())
        parsed = {"norms": [_norm_title(n) for n in norms], "number": number, "text": rest or None}
        if not (norms or number or rest):
            # Без условий это не поиск, а листание всего корпуса (как у MCP).
            return {"parsed": parsed, "total": 0, "rows": [], "has_more": False, "empty": True}

        query = Query(
            courts=INSTANCES.get(instance, ()),
            number=number,
            text=rest or None,
            norms=tuple(norms),
            sort="date" if sort == "date" else "relevance",
            decided_from=date_from,
            decided_to=date_to,
            limit=limit,
            offset=offset,
            count_cap=COUNT_CAP,
        )
        only_higher = bool(query.courts) and all(c in HIGHER for c in query.courts)
        rows: list[dict] = []
        higher_total = 0
        cases_total: int | None = 0
        texts_share = 0.0
        capped = False
        case_rows: list = []

        # Над основной выдачей — акты ВС и КС, по пять, только на первой
        # странице: смешивать два источника под одним `offset` нельзя.
        if only_higher or offset == 0:
            top_query = query if only_higher else replace(query, limit=HIGHER_TOP)
            for domain in wants_higher(query):
                top = run_higher(engine, top_query, domain)
                higher_total += top.total
                texts_share = top.texts_share
                for row in top.rows:
                    rows.append(
                        {
                            "ref": f"{'v' if domain == 'www.vsrf.ru' else 'k'}|{row['pdf_id']}",
                            "court": domain,
                            "court_title": HIGHER[domain].title,
                            "number": row["number"],
                            "date": row["decision_date"],
                            "outcome": row["kind"],
                            "about": row["about"],
                            "snippet": row.get("snippet"),
                            "url": row["url"],
                            "has_act": True,
                        }
                    )
        if not only_higher:
            found_cases = run(engine, query)
            cases_total = found_cases.total
            capped = found_cases.capped
            texts_share = found_cases.texts_share if (rest or norms) else texts_share
            case_rows = found_cases.rows
            for row in case_rows:
                rows.append(
                    {
                        "ref": f"c|{row['court_domain']}|{row['case_number']}",
                        "court": row["court_domain"],
                        "court_title": court_title(row["court_domain"]),
                        "number": row["case_number"],
                        "date": row["decision_date"],
                        "outcome": row["result"],
                        "judge": row["judge"],
                        "snippet": row.get("snippet"),
                        "url": row["card_url"],
                        "has_act": row["act_published"],
                        "in_supreme": row["in_supreme"],
                    }
                )
        return {
            "parsed": parsed,
            # `None` — счёт «слишком много, считать не стали»: страница
            # пишет «много», а не выдумывает число.
            "total": None if cases_total is None else cases_total + higher_total,
            "capped": capped,
            "cases_total": cases_total,
            "higher_total": higher_total,
            "rows": rows,
            "has_more": not only_higher and len(case_rows) == limit,
            # Доля актов, чей текст разобран, — потолок поиска по словам.
            # Для поиска только по реквизитам она не имеет смысла.
            "texts_share": texts_share if (rest or norms) else None,
        }

    @app.get("/api/suggest")
    def suggest(q: str = "") -> list[dict]:
        q = q.strip()
        out: list[dict] = []
        norms, _ = split_query(q)
        out += [
            {"kind": "Норма", "text": _norm_title(n), "hint": "", "q": _norm_title(n)}
            for n in norms
        ]
        if len(q) >= 3 and any(ch.isdigit() for ch in q) and not norms:
            with engine.connect() as connection:
                for row in connection.execute(
                    select(case.c.case_number, case.c.court_domain, func.max(case.c.decision_date))
                    .where(case.c.case_number.ilike(f"%{q}%"))
                    .group_by(case.c.case_number, case.c.court_domain)
                    .order_by(func.max(case.c.decision_date).desc().nulls_last())
                    .limit(5)
                ):
                    out.append(
                        {
                            "kind": "Дело",
                            "text": row.case_number,
                            "hint": court_title(row.court_domain),
                            "q": row.case_number,
                        }
                    )
        return out

    @app.get("/api/act")
    def get_act(ref: str) -> dict:
        """Акт целиком по `ref` из выдачи: `c|<суд>|<номер>`, `v|<id>`, `k|<id>`."""
        kind, _, rest = ref.partition("|")
        if kind in ("v", "k") and rest.isdigit():
            domain = "www.vsrf.ru" if kind == "v" else "www.ksrf.ru"
            spec = HIGHER[domain]
            with engine.connect() as connection:
                row = connection.execute(
                    select(
                        spec.head.c.number,
                        spec.decided.label("date"),
                        spec.kind.label("kind"),
                        spec.about.label("about"),
                        spec.body.c.plain_text,
                    )
                    .select_from(
                        spec.head.join(spec.body, spec.body.c.pdf_id == spec.head.c.pdf_id)
                    )
                    .where(spec.head.c.pdf_id == int(rest))
                ).first()
            if row is None:
                raise HTTPException(404, "акт не найден или текст ещё не разобран")
            return {
                "ref": ref,
                "court": domain,
                "court_title": spec.title,
                "number": row.number,
                "date": row.date,
                "outcome": row.kind,
                "about": row.about,
                "url": spec.url.format(rest),
                "texts": [{"kind": row.kind, "text": row.plain_text}],
                "instances": instances(engine, uid_of_supreme_act(engine, int(rest)))
                if kind == "v"
                else [],
            }
        if kind == "c":
            domain, _, number = rest.partition("|")
            with engine.connect() as connection:
                rows = connection.execute(
                    select(
                        case.c.judge,
                        case.c.result,
                        case.c.decision_date,
                        case.c.card_url,
                        case.c.judicial_uid,
                        act.c.kind,
                        act_text.c.plain_text,
                    )
                    .select_from(
                        case.join(act, act.c.case_pk == case.c.id).join(
                            act_text, act_text.c.act_id == act.c.id
                        )
                    )
                    .where(case.c.court_domain == domain, case.c.case_number == number)
                    .order_by(case.c.decision_date.desc().nulls_last(), act.c.text_number)
                ).all()
            if not rows:
                # Два разных «нет»: дела нет в корпусе или его текст не разобран.
                raise HTTPException(404, "акт не найден или текст ещё не разобран")
            first = rows[0]
            return {
                "ref": ref,
                "court": domain,
                "court_title": court_title(domain),
                "number": number,
                "date": first.decision_date,
                "outcome": first.result,
                "judge": first.judge,
                "url": first.card_url,
                "texts": [{"kind": r.kind, "text": r.plain_text} for r in rows],
                "instances": instances(engine, first.judicial_uid),
            }
        raise HTTPException(400, "ref не разобран")

    # Защита от DNS-rebinding: страница из чужого домена не должна дотянуться
    # до корпуса через браузер владельца. Добавляется последней — значит,
    # проверяется раньше всего остального. Публичный адрес — из окружения.
    hosts = ["localhost", "127.0.0.1", "testserver"]
    hosts += [h.strip() for h in os.environ.get("SUDRF_WEB_HOSTS", "").split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    return app
