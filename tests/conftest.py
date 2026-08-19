from __future__ import annotations

from pathlib import Path

import pytest

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
