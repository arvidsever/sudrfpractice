"""HTTP-клиент к судам: дроссель, обязательный User-Agent, cp1251, журнал.

Ни одного запроса мимо этого клиента. Дроссель тут не «вежливая настройка»,
а условие, на котором проект вообще ходит на суды; счётчик запросов и лог —
чтобы этот факт можно было проверить, а не только пообещать.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass

import httpx

from .config import Settings
from .config import settings as default_settings
from .encoding import decode

log = logging.getLogger("harvester.http")


class DailyCapReached(RuntimeError):
    """Дневной потолок запросов к суду исчерпан."""


@dataclass(slots=True)
class Response:
    url: str
    status_code: int
    content: bytes

    @property
    def text(self) -> str:
        return decode(self.content)


class CourtClient:
    """Синхронный клиент с дросселем на хост.

    Дроссель именно на хост, а не глобальный: обход нескольких судов
    параллельно не должен превращаться в очередь, но и разгонять один суд
    нельзя.
    """

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or default_settings
        self._last_request: dict[str, float] = defaultdict(float)
        self._requests_today: dict[str, int] = defaultdict(int)
        self._client = client or httpx.Client(
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru,en;q=0.8",
            },
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        )

    def __enter__(self) -> CourtClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self, host: str) -> None:
        elapsed = time.monotonic() - self._last_request[host]
        remaining = self.settings.min_delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request[host] = time.monotonic()

    def get(self, url: str) -> Response:
        host = httpx.URL(url).host
        if self._requests_today[host] >= self.settings.daily_request_cap:
            raise DailyCapReached(
                f"{host}: исчерпан дневной потолок ({self.settings.daily_request_cap})"
            )

        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            self._throttle(host)
            self._requests_today[host] += 1
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("%s: попытка %d не удалась: %s", host, attempt, exc)
                continue

            log.info("GET %s → %d (%d байт)", url, response.status_code, len(response.content))
            if response.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    f"{response.status_code}", request=response.request, response=response
                )
                continue
            return Response(url=url, status_code=response.status_code, content=response.content)

        raise RuntimeError(f"{url}: не удалось получить ответ") from last_error

    @property
    def requests_today(self) -> dict[str, int]:
        return dict(self._requests_today)
