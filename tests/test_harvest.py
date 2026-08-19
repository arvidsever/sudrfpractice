"""Обход окна — на фикстуре, без сети.

Раннер проверяется до того, как он пойдёт в суд: журнал полноты и upsert
должны работать раньше, чем появится первый живой запрос.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select

from harvester.db.schema import act, case, harvest_run, raw_page
from harvester.directories import cartoteka, court
from harvester.harvest import harvest_listing
from harvester.http import Response
from harvester.urls import DateAxis

WINDOW = (date(2026, 6, 1), date(2026, 6, 7))


@pytest.fixture
def offline_client(monkeypatch, listing_acts: str):
    """Подменяет сеть фикстурой. Байты — в cp1251, как их отдаёт суд."""
    payload = listing_acts.encode("cp1251", errors="replace")

    def fake_get(self, url: str) -> Response:
        return Response(url=url, status_code=200, content=payload)

    monkeypatch.setattr("harvester.http.CourtClient.get", fake_get)


def _harvest(settings, **kwargs):
    return harvest_listing(
        court("2kas.sudrf.ru"),
        cartoteka("g3"),
        DateAxis.PUBLICATION,
        *WINDOW,
        settings=settings,
        bulk=False,
        **kwargs,
    )


def test_one_page_writes_cases_acts_and_raw(db_settings, offline_client) -> None:
    result = _harvest(db_settings, max_pages=1)

    assert result.expected == 530
    assert result.cases == 25
    assert result.acts == 25
    assert result.pages == 1

    engine = create_engine(db_settings.database_url)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(case)).scalar_one() == 25
        assert connection.execute(select(func.count()).select_from(act)).scalar_one() == 25
        assert connection.execute(select(func.count()).select_from(raw_page)).scalar_one() == 1
    engine.dispose()

    assert list(db_settings.raw_root.rglob("*.html.zst"))


def test_incomplete_window_is_recorded_not_hidden(db_settings, offline_client) -> None:
    """Счётчик обещал 530, обошли одну страницу — это `pilot`, а не «готово».

    Молчаливое «собрали 25 дел» здесь было бы худшим исходом: недосбор
    не проявляется как ошибка и ловится только этим журналом.
    """
    result = _harvest(db_settings, max_pages=1)
    assert result.status == "pilot"

    engine = create_engine(db_settings.database_url)
    with engine.connect() as connection:
        row = connection.execute(
            select(
                harvest_run.c.expected_count,
                harvest_run.c.fetched_rows,
                harvest_run.c.status,
                harvest_run.c.finished_at,
            )
        ).one()
    engine.dispose()

    assert row.expected_count == 530
    assert row.fetched_rows == 25
    assert row.status == "pilot"
    assert row.finished_at is not None


def test_second_pass_updates_and_does_not_duplicate(db_settings, offline_client) -> None:
    """Окна будут перечитываться — из-за доработки парсера, обрыва связи
    или нового акта у старого дела. Второй проход обязан обновлять."""
    _harvest(db_settings, max_pages=1)
    _harvest(db_settings, max_pages=1)

    engine = create_engine(db_settings.database_url)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(case)).scalar_one() == 25
        assert connection.execute(select(func.count()).select_from(act)).scalar_one() == 25
        assert connection.execute(select(func.count()).select_from(harvest_run)).scalar_one() == 2
    engine.dispose()


def test_dates_and_act_flag_are_stored(db_settings, offline_client) -> None:
    _harvest(db_settings, max_pages=1)

    engine = create_engine(db_settings.database_url)
    with engine.connect() as connection:
        row = connection.execute(
            select(case).where(case.c.case_uid == "3128af6a-aafd-43ab-a873-18c4f46b860e")
        ).one()
    engine.dispose()

    assert row.case_number == "8Г-15211/2026 [88-14715/2026]"
    assert row.receipt_date == date(2026, 4, 28)
    assert row.decision_date == date(2026, 5, 14)
    # 262-ФЗ: признак публикации хранится явно, а не выводится из пустой таблицы.
    assert row.act_published is True


def test_missing_act_link_is_unknown_not_denial(
    db_settings, monkeypatch, listing_appeal_delo_id: str
) -> None:
    """Пустая последняя колонка означает «не знаем», а не «акта нет».

    `false` здесь было бы ложью: колонка бывает пустой и потому, что перечень
    запрошен с коротким `delo_id`. Акт при этом опубликован.
    """
    payload = listing_appeal_delo_id.encode("cp1251", errors="replace")
    monkeypatch.setattr(
        "harvester.http.CourtClient.get",
        lambda self, url: Response(url=url, status_code=200, content=payload),
    )

    _harvest(db_settings, max_pages=1)

    engine = create_engine(db_settings.database_url)
    with engine.connect() as connection:
        published = connection.execute(select(case.c.act_published)).scalars().all()
    engine.dispose()

    assert len(published) == 25
    assert all(value is None for value in published)
