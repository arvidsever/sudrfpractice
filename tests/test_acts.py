"""Сбор текстов актов — на фикстуре, без сети."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, insert, select

from harvester.acts import collect_act_texts, pending_acts
from harvester.config import Settings
from harvester.db.schema import act, act_text, case
from harvester.http import Response


@pytest.fixture
def seeded(db_settings: Settings) -> Settings:
    """Одно дело с одной ссылкой на акт — минимум, на котором виден свод."""
    engine = create_engine(db_settings.database_url)
    with engine.begin() as connection:
        case_pk = connection.execute(
            insert(case)
            .values(
                court_domain="2kas.sudrf.ru",
                cartoteka_id="g3",
                case_uid="3128af6a-aafd-43ab-a873-18c4f46b860e",
                case_number="8Г-15211/2026 [88-14715/2026]",
            )
            .returning(case.c.id)
        ).scalar_one()
        connection.execute(
            insert(act).values(
                case_pk=case_pk,
                doc_number="18565938",
                text_number=1,
                kind="Постановления",
                url="https://2kas.sudrf.ru/modules.php?name_op=doc&number=18565938",
            )
        )
    engine.dispose()
    return db_settings


def _serve(monkeypatch, html: str) -> None:
    payload = html.encode("cp1251", errors="replace")
    monkeypatch.setattr(
        "harvester.http.CourtClient.get",
        lambda self, url: Response(url=url, status_code=200, content=payload),
    )


def test_text_is_stored_and_matches_the_card(seeded, monkeypatch, act_doc, case_card) -> None:
    from harvester.parse.act import parse_card_act

    _serve(monkeypatch, act_doc)
    result = collect_act_texts("2kas.sudrf.ru", settings=seeded, bulk=False)

    assert (result.stored, result.empty, result.failed, result.remaining) == (1, 0, 0, 0)

    engine = create_engine(seeded.database_url)
    with engine.connect() as connection:
        stored = connection.execute(select(act_text.c.plain_text)).scalar_one()
        published = connection.execute(select(case.c.act_published)).scalar_one()
    engine.dispose()

    # Тот же текст, что во вкладке карточки: одного запроса на дело хватает.
    assert stored == parse_card_act(case_card, text_number=1)
    assert published is True


def test_second_sweep_takes_nothing(seeded, monkeypatch, act_doc) -> None:
    """Свод не качает то, что уже скачано: выборка идёт по отсутствию текста,
    поэтому прерванный сбор продолжается сам, без отдельного учёта."""
    _serve(monkeypatch, act_doc)
    collect_act_texts("2kas.sudrf.ru", settings=seeded, bulk=False)
    again = collect_act_texts("2kas.sudrf.ru", settings=seeded, bulk=False)

    assert (again.attempted, again.stored, again.remaining) == (0, 0, 0)

    engine = create_engine(seeded.database_url)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(act_text)).scalar_one() == 1
    engine.dispose()


def test_empty_document_is_not_written(seeded, monkeypatch) -> None:
    """«ПУСТОЙ ДОКУМЕНТ» — признак конца перебора `text_number`. В базу он
    не идёт: пусть акт остаётся видимым как несделанная работа."""
    _serve(monkeypatch, "<html><body><div id='content'>ПУСТОЙ ДОКУМЕНТ</div></body></html>")
    result = collect_act_texts("2kas.sudrf.ru", settings=seeded, bulk=False)

    assert (result.stored, result.empty) == (0, 1)
    assert result.remaining == 1


def test_throttling_stops_the_sweep(seeded, monkeypatch, temporarily_unavailable) -> None:
    """Суд попросил перестать — продолжать значит копить блокировку."""
    _serve(monkeypatch, temporarily_unavailable)
    result = collect_act_texts("2kas.sudrf.ru", settings=seeded, bulk=False)

    assert result.throttled
    assert result.stored == 0
    assert result.remaining == 1


def test_pending_is_the_ledger(seeded) -> None:
    """Полнота свода измеряется базой, а не памятью о прогоне."""
    engine = create_engine(seeded.database_url)
    with engine.connect() as connection:
        assert len(pending_acts(connection, "2kas.sudrf.ru")) == 1
        assert len(pending_acts(connection, "5kas.sudrf.ru")) == 0
    engine.dispose()
