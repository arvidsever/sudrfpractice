"""HTTP-клиент к судам: дроссель, обязательный User-Agent, cp1251, журнал.

Ни одного запроса мимо этого клиента. Дроссель тут не «вежливая настройка»,
а условие, на котором проект вообще ходит на суды; счётчик запросов и лог —
чтобы этот факт можно было проверить, а не только пообещать.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from .config import Settings
from .config import settings as default_settings
from .encoding import decode

log = logging.getLogger("harvester.http")


class DailyCapReached(RuntimeError):
    """Дневной потолок запросов к суду исчерпан."""


class OutsideCollectionWindow(RuntimeError):
    """Массовый обход запущен вне ночного окна."""


class CaptchaNotPassed(RuntimeError):
    """Суд с капчей не пропустил нас после всех попыток."""


class CourtOnCooldown(RuntimeError):
    """Суд просил отступить, и срок паузы ещё не вышел."""


def within_night_window(window: tuple[int, int], moment: datetime | None = None) -> bool:
    """Попадает ли момент в окно сбора [от, до) по часам.

    Окно может пересекать полночь (23–5), поэтому сравнение не сплошное.
    """
    start, end = window
    hour = (moment or datetime.now()).hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


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

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        *,
        bulk: bool = False,
        captcha: object | None = None,
        captcha_form_url: object | None = None,
    ):
        """`bulk=True` — режим массового обхода: он и только он обязан идти
        в ночное окно. Единичный диагностический запрос под это правило
        не подпадает, иначе проверить суд днём стало бы невозможно."""
        self.settings = settings or default_settings
        self.bulk = bulk
        #: Решатель капчи и способ узнать, где у суда лежит форма с ней.
        #: Без них клиент на капча-судах просто вернёт страницу-заглушку,
        #: и это увидит `guards.classify` — молча ничего не потеряется.
        self.captcha = captcha
        self.captcha_form_url = captcha_form_url
        #: До какого момента суд трогать нельзя. Пауза общая на процесс:
        #: следующая задача не должна начинать с того, чем предыдущая
        #: только что заслужила отказ.
        self._cooldown_until: dict[str, float] = _COOLDOWNS
        # Состояние по хосту — общее на процесс, а не на экземпляр.
        # Клиент создаётся на каждое окно обхода, и если бы дроссель жил
        # в экземпляре, соседние окна одного суда стреляли бы подряд
        # без паузы: тысяча окон превратилась бы в тысячу таких стыков.
        self._last_request = _LAST_REQUEST
        self._requests_today = _REQUESTS_TODAY
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
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        remaining = self.settings.min_delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request[host] = time.monotonic()

    def cooldown_left(self, host: str) -> float:
        """Сколько секунд ещё нельзя трогать этот суд."""
        return max(0.0, self._cooldown_until.get(host, 0.0) - time.monotonic())

    def back_off(self, host: str, seconds: float, reason: str) -> None:
        """Отступить от суда на заданный срок.

        Продолжать стучаться после отказа — это и есть накопление
        блокировки, от которого предупреждает §6 грамматики.
        """
        self._cooldown_until[host] = time.monotonic() + seconds
        log.warning("%s: пауза %.0f мин — %s", host, seconds / 60, reason)

    def get(self, url: str) -> Response:
        host = httpx.URL(url).host
        left = self.cooldown_left(host)
        if left > 0:
            raise CourtOnCooldown(f"{host}: ещё {left / 60:.0f} мин паузы")
        if self.bulk and not within_night_window(self.settings.night_window):
            start, end = self.settings.night_window
            raise OutsideCollectionWindow(
                f"массовый обход разрешён только с {start}:00 до {end}:00"
            )
        if self._requests_today.get(host, 0) >= self.settings.daily_request_cap:
            raise DailyCapReached(
                f"{host}: исчерпан дневной потолок ({self.settings.daily_request_cap})"
            )

        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            with host_lock(host):
                self._throttle(host)
                self._requests_today[host] = self._requests_today.get(host, 0) + 1
                try:
                    response = self._client.get(url)
                except httpx.HTTPError as exc:
                    last_error = exc
                    log.warning("%s: попытка %d не удалась: %s", host, attempt, exc)
                    continue

                log.info("GET %s → %d (%d байт)", url, response.status_code, len(response.content))
                if _looks_throttled(response.content):
                    self.back_off(host, self.settings.cooldown_seconds, "суд придержал адрес")
                if response.status_code >= 500:
                    last_error = httpx.HTTPStatusError(
                        f"{response.status_code}", request=response.request, response=response
                    )
                    continue
                return Response(url=url, status_code=response.status_code, content=response.content)

        raise RuntimeError(f"{url}: не удалось получить ответ") from last_error

    def get_passing_captcha(self, url: str, attempts: int = 3) -> Response:
        """Как `get`, но на капча-судах проходит капчу и повторяет запрос.

        Перебираются ПРОЧТЕНИЯ одной картинки, а не картинки: портал держит
        капчу на IP несколько минут и от неверных ответов не меняет —
        проверено, три захода за формой подряд дают тот же `captchaid`
        и те же байты. Значит новая форма ничего не даёт, а вот второй
        по вероятности вариант цифры — даёт.
        """
        from .captcha.gate import extract_challenge
        from .guards import Verdict, classify

        host = httpx.URL(url).host
        solver = self.captcha

        token = solver.cached(host) if solver is not None else None
        response = self.get(_with_token(url, token) if token else url)
        if classify(response.text).verdict is not Verdict.CAPTCHA_GATE:
            return response
        if solver is None or self.captcha_form_url is None:
            return response

        solver.forget(host)
        form = self.get(self.captcha_form_url(url))
        challenge = extract_challenge(form.text)
        if challenge is None:
            log.warning("%s: в форме нет капчи, хотя выдача её требует", host)
            return response

        readings = solver.read(challenge, limit=attempts)
        for number, (text, likelihood) in enumerate(readings, start=1):
            candidate = with_captcha_params(url, text, challenge.captchaid)
            response = self.get(candidate)
            if classify(response.text).verdict is not Verdict.CAPTCHA_GATE:
                solver.accept(host, challenge, text)
                log.info(
                    "%s: капча пройдена прочтением %s (вариант %d из %d)",
                    host,
                    text,
                    number,
                    len(readings),
                )
                return response
            log.info("%s: прочтение %s не подошло (правдоподобие %.3f)", host, text, likelihood)

        # Картинка не сменится ещё несколько минут, а значит и прочтения
        # будут те же. Отступаем, вместо того чтобы копить отказы.
        self.back_off(host, self.settings.captcha_cooldown_seconds, "капча не поддалась")
        raise CaptchaNotPassed(f"{host}: ни одно из {len(readings)} прочтений капчи не подошло")

    @property
    def requests_today(self) -> dict[str, int]:
        return dict(self._requests_today)


def _with_token(url: str, token) -> str:
    return with_captcha_params(url, token.text, token.captchaid)


def with_captcha_params(url: str, text: str, captchaid: str) -> str:
    from .urls import with_captcha

    return with_captcha(url, text, captchaid)


#: Всё состояние по хосту — общее на процесс: клиенты создаются на каждую
#: задачу, а суд один. Дроссель, счётчик и пауза обязаны его переживать.
_COOLDOWNS: dict[str, float] = {}
_LAST_REQUEST: dict[str, float] = {}
_REQUESTS_TODAY: dict[str, int] = {}

#: По замку на суд. Обход идёт в несколько потоков — по одному на суд, —
#: но замок нужен и на случай, если два потока всё же сойдутся на одном
#: хосте: дроссель без него превращается в «оба подождали и оба выстрелили».
_HOST_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def host_lock(host: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _HOST_LOCKS.setdefault(host, threading.Lock())


_THROTTLE_MARKER = "Информация временно недоступна".encode("cp1251")


def _looks_throttled(content: bytes) -> bool:
    """Дешёвая проверка до разбора: маркер ищется прямо в байтах cp1251."""
    return _THROTTLE_MARKER in content
