"""Веб-интерфейс поиска: тонкий слой над `search`, поэтому проверяем швы —
разбор строки, склейку ВС/КС с основной выдачей и честность «нет»."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from harvester.norms import split_query
from test_search import _seed, _seed_higher, _seed_texts

# FastAPI ставится только там, где поднят интерфейс (`.[api]`), как и `mcp`.
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from harvester.web import create_app  # noqa: E402


def _client(db_settings) -> tuple[TestClient, object]:
    engine = create_engine(db_settings.database_url)
    _seed(db_settings)
    _seed_texts(engine)
    _seed_higher(engine)
    return TestClient(create_app(engine)), engine


def test_split_query_cuts_norm_out_of_words() -> None:
    """Номер статьи в полнотекстовом запросе — лишнее сужение цифрами."""
    assert split_query("ст. 333 ГК РФ снижение неустойки") == (["ГК:333"], "снижение неустойки")
    assert split_query("снижение неустойки") == ([], "снижение неустойки")


def test_search_by_norm_and_words_reports_parsed_query(db_settings) -> None:
    client, engine = _client(db_settings)

    body = client.get("/api/search", params={"q": "неустойка"}).json()

    assert body["parsed"]["text"] == "неустойка"
    assert body["total"] == body["cases_total"] + body["higher_total"]
    assert body["texts_share"] == 1.0, "потолок поиска по словам назван в ответе"
    courts = {row["court"] for row in body["rows"]}
    assert {"www.vsrf.ru", "www.ksrf.ru", "5kas.sudrf.ru"} <= courts
    engine.dispose()


def test_empty_query_is_not_a_browse_of_the_corpus(db_settings) -> None:
    client, engine = _client(db_settings)

    body = client.get("/api/search", params={"q": "  "}).json()

    assert body["rows"] == [] and body["empty"] is True
    engine.dispose()


def test_nothing_found_is_an_answer_not_an_error(db_settings) -> None:
    client, engine = _client(db_settings)

    response = client.get("/api/search", params={"q": "расторжение доставки пиццы"})

    assert response.status_code == 200
    assert response.json()["total"] == 0
    engine.dispose()


def test_instance_filter_keeps_higher_courts_out_of_cassation(db_settings) -> None:
    client, engine = _client(db_settings)

    body = client.get("/api/search", params={"q": "неустойка", "instance": "kas"}).json()

    assert body["higher_total"] == 0
    assert all(row["court"].endswith("kas.sudrf.ru") for row in body["rows"])
    engine.dispose()


def test_act_by_ref_roundtrip(db_settings) -> None:
    client, engine = _client(db_settings)

    rows = client.get("/api/search", params={"q": "неустойка"}).json()["rows"]
    for row in rows:
        body = client.get("/api/act", params={"ref": row["ref"]}).json()
        assert body["number"] == row["number"]
        assert body["texts"][0]["text"], row["ref"]
    engine.dispose()


def test_act_of_case_without_text_is_404_not_empty_text(db_settings) -> None:
    """Текст не разобран — это «нет текста», а не акт из пустой строки."""
    client, engine = _client(db_settings)

    response = client.get("/api/act", params={"ref": "c|5kas.sudrf.ru|77-3/2026"})

    assert response.status_code == 404
    engine.dispose()


def test_foreign_host_is_refused(db_settings) -> None:
    """DNS-rebinding: страница чужого домена не должна дотянуться до корпуса."""
    client, engine = _client(db_settings)

    assert client.get("/api/search?q=x", headers={"host": "evil.example"}).status_code == 400
    engine.dispose()


def test_writes_are_impossible_on_the_default_connection() -> None:
    """Только чтение — свойство соединения, а не аккуратности кода."""
    import inspect

    from harvester import web

    assert "default_transaction_read_only=on" in inspect.getsource(web._connect)


def test_statement_timeout_is_an_answer_for_the_page(db_settings, monkeypatch) -> None:
    """Потолок в 30 с достижим: страница получает объяснение, а не 500."""
    from sqlalchemy.exc import OperationalError

    import harvester.web as web

    client, engine = _client(db_settings)

    def too_slow(*_, **__):
        raise OperationalError(
            "select", {}, Exception("canceling statement due to statement timeout")
        )

    monkeypatch.setattr(web, "run", too_slow)
    response = client.get("/api/search", params={"q": "неустойка", "instance": "kas"})

    assert response.status_code == 503
    assert "30 секунд" in response.json()["detail"]
    engine.dispose()


def test_unknown_total_is_null_not_an_invented_number(db_settings, monkeypatch) -> None:
    """Счёт, не уложившийся в бюджет, — «много», а не 0 и не потолок."""
    from dataclasses import replace

    import harvester.web as web

    client, engine = _client(db_settings)
    real = web.run

    def unknown(*args, **kwargs):
        return replace(real(*args, **kwargs), total=None, capped=True)

    monkeypatch.setattr(web, "run", unknown)
    body = client.get("/api/search", params={"q": "неустойка", "instance": "kas"}).json()

    assert body["total"] is None and body["capped"] is True
    assert body["rows"], "строки при этом показываются"
    engine.dispose()


# --- пароль на входе ---------------------------------------------------------


def _locked(db_settings, *, secure: bool = False):
    from harvester.web import auth as authlib

    engine = create_engine(db_settings.database_url)
    _seed(db_settings)
    _seed_texts(engine)
    guard = authlib.Auth(authlib.hash_password("correct horse battery"), "k" * 40, secure)
    return TestClient(create_app(engine, auth=guard), follow_redirects=False), guard, engine


def test_without_cookie_everything_but_login_is_closed(db_settings) -> None:
    client, _, engine = _locked(db_settings)

    page = client.get("/")
    assert page.status_code == 303 and page.headers["location"] == "/login"
    api = client.get("/api/search", params={"q": "неустойка"})
    assert api.status_code == 401, "API не отдаёт строк без входа"
    assert client.get("/api/act", params={"ref": "c|x|y"}).status_code == 401
    assert client.get("/login").status_code == 200
    engine.dispose()


def test_login_sets_httponly_cookie_and_opens_the_site(db_settings, monkeypatch) -> None:
    import asyncio

    async def no_wait(_):  # не ждём секунду на каждый тест
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    client, _, engine = _locked(db_settings, secure=True)

    wrong = client.post("/login", content="password=nope")
    assert wrong.status_code == 401 and "sudrf_session" not in wrong.headers.get("set-cookie", "")

    ok = client.post("/login", content="password=correct+horse+battery")
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie

    opened = TestClient(client.app, follow_redirects=False, base_url="https://testserver")
    opened.cookies.set("sudrf_session", ok.cookies["sudrf_session"])
    assert opened.get("/api/search", params={"q": "неустойка"}).status_code == 200
    assert opened.get("/").headers["content-security-policy"].startswith("default-src 'self'")
    engine.dispose()


def test_forged_and_expired_cookies_are_refused() -> None:
    import time

    from harvester.web import auth as authlib

    guard = authlib.Auth(authlib.hash_password("x" * 12), "k" * 40)
    good = guard.make_cookie()
    assert guard.valid_cookie(good)
    expires, _, signature = good.partition(".")
    assert not guard.valid_cookie(f"{int(expires) + 999999}.{signature}"), "срок не продлить"
    assert not guard.valid_cookie(f"{expires}.{'0' * 64}")
    assert not guard.valid_cookie(None) and not guard.valid_cookie("мусор")
    other = authlib.Auth(guard.password_hash, "z" * 40)
    assert not other.valid_cookie(good), "cookie с чужим ключом не годится"
    assert not guard.valid_cookie(good, now=time.time() + 15 * 86400), "cookie устаревает"


def test_login_is_throttled_per_client(db_settings, monkeypatch) -> None:
    import asyncio

    async def no_wait(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    client, _, engine = _locked(db_settings)

    for _ in range(5):
        assert client.post("/login", content="password=bad").status_code == 401
    blocked = client.post("/login", content="password=correct+horse+battery")

    assert blocked.status_code == 429, "после пяти промахов и верный пароль не принимается"
    engine.dispose()


def test_public_mode_refuses_to_start_without_a_password(monkeypatch) -> None:
    from harvester.web import auth as authlib

    assert authlib.from_env({}) is None, "локальный режим — без пароля"
    with pytest.raises(RuntimeError):
        authlib.from_env({"SUDRF_WEB_REQUIRE_AUTH": "1"})
    with pytest.raises(RuntimeError):
        authlib.from_env({"SUDRF_WEB_PASSWORD_HASH": "x", "SUDRF_WEB_SESSION_KEY": "коротко"})
    assert authlib.check_password(
        "правильный пароль 1", authlib.hash_password("правильный пароль 1")
    )
    assert not authlib.check_password("другой", authlib.hash_password("правильный пароль 1"))
