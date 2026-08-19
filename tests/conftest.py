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
def listing_no_act_links() -> str:
    """То же окно и тот же суд, снято 19.08.2026: последняя колонка пуста.

    06.08.2026 ссылка на текст была у каждой строки. 19.08.2026 её нет
    ни у одной — ни на другом суде, ни в другой картотеке, ни в другом окне.
    Счётчик, число строк и колонок при этом те же.
    """
    return fixture("ksoyu_listing_no_act_links")


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
