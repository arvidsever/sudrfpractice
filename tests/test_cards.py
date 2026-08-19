"""Свод карточек — на фикстурах, без сети."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, insert, select

from harvester.cards import collect_cards, pending_cards
from harvester.config import Settings
from harvester.db.schema import act, act_text, appeal, case, hearing, participant
from harvester.http import Response


@pytest.fixture
def seeded(db_settings: Settings) -> Settings:
    engine = create_engine(db_settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            insert(case).values(
                court_domain="5kas.sudrf.ru",
                cartoteka_id="g3",
                case_id="15775812",
                case_uid="adf9361e-3225-4ea0-a714-4f5d49ce4b9d",
                case_number="8Г-23799/2025 [88-21481/2025]",
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


def test_one_card_brings_text_and_everything_else(seeded, monkeypatch, case_card) -> None:
    """Ради этого свод и переехал с `name_op=doc` на карточку: тот же один
    запрос, а сверх текста — участники, движение и первая инстанция."""
    _serve(monkeypatch, case_card)
    result = collect_cards("5kas.sudrf.ru", settings=seeded, bulk=False)

    assert (result.cards, result.texts, result.participants) == (1, 1, 2)
    assert result.remaining == 0

    engine = create_engine(seeded.database_url)
    with engine.connect() as connection:
        row = connection.execute(select(case)).one()
        assert connection.execute(select(func.count()).select_from(participant)).scalar_one() == 2
        assert connection.execute(select(func.count()).select_from(hearing)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(appeal)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(act_text)).scalar_one() == 1
    engine.dispose()

    assert row.lower_court == "Перовский районный суд (Город Москва)"
    assert row.act_published is True
    assert row.card_fetched_at is not None


def test_act_identity_rests_on_the_tab_number(seeded, monkeypatch, case_card) -> None:
    """Карточка не показывает номер документа, а дела с оси поступления
    приходят без ссылки на акт. Значит акт опознаётся номером вкладки."""
    _serve(monkeypatch, case_card)
    collect_cards("5kas.sudrf.ru", settings=seeded, bulk=False)

    engine = create_engine(seeded.database_url)
    with engine.connect() as connection:
        row = connection.execute(select(act)).one()
    engine.dispose()

    assert row.text_number == 1
    assert row.doc_number is None


def test_second_sweep_takes_nothing(seeded, monkeypatch, case_card) -> None:
    _serve(monkeypatch, case_card)
    collect_cards("5kas.sudrf.ru", settings=seeded, bulk=False)
    again = collect_cards("5kas.sudrf.ru", settings=seeded, bulk=False)

    assert (again.attempted, again.cards) == (0, 0)


def test_participants_are_rewritten_not_appended(seeded, monkeypatch, case_card) -> None:
    """Карточка — источник истины. Частичное обновление оставляло бы
    призраков от прошлых разборов."""
    _serve(monkeypatch, case_card)
    collect_cards("5kas.sudrf.ru", settings=seeded, bulk=False)

    engine = create_engine(seeded.database_url)
    with engine.begin() as connection:
        connection.execute(case.update().values(card_fetched_at=None))
    collect_cards("5kas.sudrf.ru", settings=seeded, bulk=False)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(participant)).scalar_one() == 2
    engine.dispose()


def test_card_without_text_is_knowledge_too(seeded, monkeypatch) -> None:
    """262-ФЗ: публикуется не всё. Карточку открыли — значит про акт мы
    теперь знаем, и `false` здесь честен, в отличие от пустой колонки перечня."""
    _serve(
        monkeypatch,
        "<html><body><table><tr><th>ДЕЛО</th></tr>"
        "<tr><td><b>Судья</b></td><td>Иванов И. И.</td></tr></table></body></html>",
    )
    result = collect_cards("5kas.sudrf.ru", settings=seeded, bulk=False)

    assert (result.cards, result.texts, result.without_text) == (1, 0, 1)

    engine = create_engine(seeded.database_url)
    with engine.connect() as connection:
        assert connection.execute(select(case.c.act_published)).scalar_one() is False
    engine.dispose()


def test_throttling_stops_the_sweep(seeded, monkeypatch, temporarily_unavailable) -> None:
    _serve(monkeypatch, temporarily_unavailable)
    result = collect_cards("5kas.sudrf.ru", settings=seeded, bulk=False)

    assert result.throttled
    assert result.cards == 0
    assert result.remaining == 1


def test_pending_needs_identifiers(seeded) -> None:
    """Без case_id и case_uid карточку не собрать — такие дела в свод
    не берутся, а не падают в нём."""
    engine = create_engine(seeded.database_url)
    with engine.begin() as connection:
        connection.execute(
            insert(case).values(
                court_domain="5kas.sudrf.ru", cartoteka_id="g3", case_number="без идентификаторов"
            )
        )
    with engine.connect() as connection:
        assert len(pending_cards(connection, "5kas.sudrf.ru")) == 1
    engine.dispose()
