"""Прохождение капчи на форме поиска.

Механика портала (§6 грамматики):

* картинка приходит **инлайном** в форме как `data:image/png;base64,…`,
  рядом лежит `<input name="captchaid">` — отдельного запроса за картинкой нет;
* одна капча держится на IP несколько минут и **не меняется от неверных
  ответов**, поэтому перебор бесполезен;
* решённая пара действует на весь регион около шести часов.

Срок жизни пары здесь — оптимизация, а не условие правильности: протухшая
пара даёт тот же `CAPTCHA_GATE`, и решатель просто берёт свежую. Поэтому
по умолчанию он короче заявленных шести часов.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from selectolax.parser import HTMLParser

from .model import CaptchaModel
from .preprocess import captcha_vector, png_from_data_uri
from .solve import solve

log = logging.getLogger("harvester.captcha")

_DATA_URI = re.compile(r"^data:\s*image/", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CaptchaChallenge:
    png: bytes
    captchaid: str


@dataclass(frozen=True, slots=True)
class CaptchaToken:
    text: str
    captchaid: str
    obtained_at: float

    def is_fresh(self, ttl_seconds: float) -> bool:
        return (time.monotonic() - self.obtained_at) < ttl_seconds


def extract_challenge(html: str) -> CaptchaChallenge | None:
    """Достать из формы картинку и `captchaid`. Нет пары — нет капчи."""
    tree = HTMLParser(html)

    field = tree.css_first("input[name=captchaid]")
    captchaid = (field.attributes.get("value") or "").strip() if field is not None else ""
    if not captchaid:
        return None

    for img in tree.css("img"):
        src = img.attributes.get("src") or ""
        if _DATA_URI.match(src):
            try:
                return CaptchaChallenge(png=png_from_data_uri(src), captchaid=captchaid)
            except Exception:  # noqa: BLE001 — битая картинка это «капчи нет»
                return None
    return None


class CaptchaSolver:
    """Решает капчу и помнит решённую пару по суду.

    Порога уверенности нет намеренно: для фонового сбора неверный ответ
    стоит одной попытки на свежей картинке, а портал капчу не жжёт.
    Отсекать по уверенности значило бы менять дешёвую ошибку на дорогое
    бездействие.
    """

    def __init__(self, model: CaptchaModel, ttl_seconds: float = 30 * 60):
        self.model = model
        self.ttl_seconds = ttl_seconds
        self._tokens: dict[str, CaptchaToken] = {}

    def cached(self, domain: str) -> CaptchaToken | None:
        token = self._tokens.get(domain)
        if token is not None and token.is_fresh(self.ttl_seconds):
            return token
        return None

    def forget(self, domain: str) -> None:
        """Пара не подошла — выбрасываем, чтобы следующий заход взял свежую."""
        self._tokens.pop(domain, None)

    def solve_challenge(self, domain: str, challenge: CaptchaChallenge) -> CaptchaToken:
        solution = solve(self.model, captcha_vector(challenge.png))
        log.info(
            "%s: капча решена как %s (уверенность %.2f)",
            domain,
            solution.text,
            solution.confidence,
        )
        token = CaptchaToken(
            text=solution.text,
            captchaid=challenge.captchaid,
            obtained_at=time.monotonic(),
        )
        self._tokens[domain] = token
        return token
