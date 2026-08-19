"""Создание клиента с учётом того, закрыт ли суд капчей.

Одно место, где решается, нужен ли решатель. Без него на капча-судах
клиент вернёт страницу-заглушку, `guards.classify` назовёт её
`CAPTCHA_GATE`, и обход честно остановится — молча ничего не потеряется.
"""

from __future__ import annotations

import logging

from .captcha.gate import CaptchaSolver
from .captcha.model import DEFAULT_PATH, load_model
from .config import Settings
from .config import settings as default_settings
from .directories import Cartoteka, Court
from .directories import cartoteka as find_cartoteka
from .http import CourtClient
from .urls import search_form_url

log = logging.getLogger("harvester.client")

_solver: CaptchaSolver | None = None


def shared_solver() -> CaptchaSolver | None:
    """Решатель на весь процесс: решённая пара переживает смену картотеки.

    Пара действует на суд целиком, поэтому держать её пер-обход значило бы
    решать капчу заново там, где этого не требуется.
    """
    global _solver
    if _solver is None:
        if not DEFAULT_PATH.exists():
            return None
        _solver = CaptchaSolver(load_model())
    return _solver


def open_client(
    court: Court,
    cartoteka: Cartoteka | None = None,
    *,
    settings: Settings | None = None,
    bulk: bool = True,
) -> CourtClient:
    settings = settings or default_settings
    if not court.has_captcha:
        return CourtClient(settings, bulk=bulk)

    solver = shared_solver()
    if solver is None:
        log.warning(
            "%s закрыт капчей, а весов нет (%s) — обход остановится на гейте",
            court.domain,
            DEFAULT_PATH,
        )
        return CourtClient(settings, bulk=bulk)

    # Форма годится любая: пара действует на суд, а не на картотеку.
    form_cartoteka = cartoteka or find_cartoteka("g3")
    return CourtClient(
        settings,
        bulk=bulk,
        captcha=solver,
        captcha_form_url=lambda _url: search_form_url(court, form_cartoteka),
    )
