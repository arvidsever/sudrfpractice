from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import create_engine, text

from harvester.config import settings

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    """Фикстуры сняты живьём 06.08.2026 со 2 КСОЮ (без капчи) и 3 КСОЮ (с капчей)
    и хранятся уже перекодированными в UTF-8 — так же, как в `Sudrf`."""
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def listing_acts() -> str:
    """Гражданская кассация 2 КСОЮ, окно по дате ПУБЛИКАЦИИ 01–07.06.2026."""
    return fixture("ksoyu_listing_acts")


@pytest.fixture(scope="session")
def listing_appeal_delo_id() -> str:
    """То же окно, что у `listing_acts`, но запрошенное с коротким `delo_id=5`.

    Портал отдаёт тот же счётчик, те же 25 строк и те же реквизиты, но
    озаглавливает выдачу «апелляция» и оставляет колонку «Судебные акты»
    пустой. Разбор — `docs/delo-id-and-act-links.md`.
    """
    return fixture("ksoyu_listing_appeal_delo_id")


@pytest.fixture(scope="session")
def listing_koap() -> str:
    """КоАП-кассация 2 КСОЮ: акт опубликован не у каждого дела."""
    return fixture("ksoyu_listing_adm33_koap")


@pytest.fixture(scope="session")
def listing_bad_new() -> str:
    """Кривой запрос: `g33_case` с `new=0`. Портал отвечает 200 и «данных нет»."""
    return fixture("ksoyu_listing_bad_new")


@pytest.fixture(scope="session")
def listing_captcha_gate() -> str:
    """3 КСОЮ без пары `captcha`+`captchaid`."""
    return fixture("ksoyu_listing_captcha_gate")


@pytest.fixture(scope="session")
def card_criminal() -> str:
    """Карточка уголовной кассации: «ЛИЦА» с перечнем статей и «СТОРОНЫ»."""
    return fixture("ksoyu_card_criminal")


@pytest.fixture(scope="session")
def card_koap() -> str:
    """Карточка КоАП: «СТОРОНЫ ПО ДЕЛУ» со статьями, без слушаний и жалоб."""
    return fixture("ksoyu_card_koap")


@pytest.fixture(scope="session")
def temporarily_unavailable() -> str:
    """Антибрутфорс-ответ суда: «Информация временно недоступна».

    Снято 19.08.2026 со 2 КСОЮ после того, как за полтора часа с него было
    выбрано больше сотни запросов — в том числе диагностических, мимо клиента
    с дросселем. Отступать надо раньше, чем суд об этом попросит.
    """
    return fixture("ksoyu_temporarily_unavailable")


@pytest.fixture(scope="session")
def act_doc() -> str:
    return fixture("ksoyu_act_doc")


@pytest.fixture(scope="session")
def case_card() -> str:
    return fixture("ksoyu_case_card")


# --- база для тестов --------------------------------------------------------
#
# Тесты пишут и чистят таблицы, поэтому идут на ОТДЕЛЬНОЙ базе. Рабочая база
# однажды уже была стёрта прогоном тестов — второй раз этому случиться нельзя.


def _test_database_url() -> sqlalchemy.URL:
    url = sqlalchemy.make_url(settings.database_url)
    return url.set(database=f"{url.database}_test")


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Создаёт базу для тестов и накатывает миграции. Нет сервера — пропуск."""
    target = _test_database_url()
    admin = target.set(database="postgres")

    try:
        engine = create_engine(admin, isolation_level="AUTOCOMMIT")
        with engine.connect() as connection:
            exists = connection.execute(
                text("select 1 from pg_database where datname = :name"),
                {"name": target.database},
            ).scalar()
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{target.database}"'))
        engine.dispose()
    except Exception as exc:  # pragma: no cover — зависит от окружения
        pytest.skip(f"сервер базы недоступен: {exc}")

    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    url = target.render_as_string(hide_password=False)
    config.attributes["sqlalchemy_url"] = url
    command.upgrade(config, "head")

    # Дела ссылаются на суд и картотеку внешними ключами — без справочников
    # в базу не записать ни одной строки.
    from harvester.db.load_directories import load

    load(url)
    return url


@pytest.fixture
def db_settings(tmp_path, test_database_url: str):
    """Чистая база и свежий каталог сырья на каждый тест.

    Отдельная база, а не рабочая: прогон тестов однажды уже стёр собранные
    данные, потому что чистил таблицы в `praktika`.
    """
    from sqlalchemy import delete

    from harvester.config import Settings
    from harvester.db.schema import (
        act,
        act_text,
        cartoteka_volume,
        case,
        harvest_run,
        harvest_task,
        raw_page,
    )

    settings = Settings(raw_root=tmp_path / "raw", database_url=test_database_url)
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        for table in (act_text, act, case, harvest_task, harvest_run, raw_page, cartoteka_volume):
            connection.execute(delete(table))
    engine.dispose()
    return settings
