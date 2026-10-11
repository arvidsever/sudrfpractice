"""Веб-интерфейс поиска: тонкий слой над `search`, поэтому проверяем швы —
разбор строки, склейку ВС/КС с основной выдачей и честность «нет»."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from harvester.norms import split_query
from harvester.web import create_app
from test_search import _seed, _seed_higher, _seed_texts


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
