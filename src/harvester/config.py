"""Настройки харвестера.

Значения по умолчанию — не «разумные умолчания», а правила уважительного
доступа, под которыми проект вообще имеет право ходить на суды. Ослаблять
их можно только осознанно и не в коде: см. `docs/ethics.md`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HARVESTER_", env_file=".env", extra="ignore")

    # Схема `postgresql+psycopg` — драйвер psycopg3; без неё SQLAlchemy ищет psycopg2.
    database_url: str = "postgresql+psycopg://localhost:5432/praktika"

    #: Слой сырых HTML — источник истины. Парсеры заведомо будут дорабатываться,
    #: и архив избавляет от повторного обхода судов.
    raw_root: Path = REPO_ROOT / "raw"

    #: Корпус капч. Каталог приложения Sudrf НЕ используем: у него свой
    #: потолок и FIFO-подрезка, см. docs/captcha-corpus.md.
    captcha_corpus: Path = REPO_ROOT / "data" / "captcha-training" / "solved"

    #: User-Agent обязателен: с дефолтным `curl/…` WAF отдаёт 403
    #: и «Данный запрос некорректен» (§1 грамматики). Контакт в UA — намеренно.
    #:
    #: Только ASCII: HTTP-заголовки кодируются latin-1, и кириллица здесь
    #: роняет запрос ещё до отправки.
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Safari/605.1.15 "
        "(sudrfpractice research harvester; arvid.sever@gmail.com)"
    )

    #: Не меньше трёх секунд между запросами к одному хосту.
    min_delay_seconds: float = Field(default=3.0, ge=3.0)

    #: Потолок запросов в сутки на суд.
    daily_request_cap: int = 5000

    #: Ночное окно сбора, часы локального времени суда [от, до).
    night_window: tuple[int, int] = (1, 7)

    #: Пауза после того, как суд ответил «Информация временно недоступна».
    #: Продолжать стучаться — значит копить блокировку (§6 грамматики).
    cooldown_seconds: float = 30 * 60

    #: Пауза после неудачи с капчей. Картинка держится на адресе несколько
    #: минут и не меняется, поэтому раньше возвращаться бессмысленно.
    captcha_cooldown_seconds: float = 10 * 60

    request_timeout_seconds: float = 30.0
    max_retries: int = 3


settings = Settings()
