"""Пароль на входе в веб-интерфейс.

Один общий пароль и одна подписанная cookie: пользователь у сервиса один,
а корпус несёт необезличенные ФИО (docs/mcp.md). Намеренные упрощения:

* **только стандартная библиотека**: `scrypt` из `hashlib`, подпись `hmac`.
  Ни базы пользователей, ни сессий на сервере — cookie сама хранит срок
  и подпись, а сменить ключ значит выбросить всех;
* **пароль до первой страницы, не после**: вошёл — cookie на `SESSION_DAYS`
  дней, дальше страницы и API открыты без повторных вопросов;
* **пароль не проходит через ассистента и не лежит в репозитории**: хеш
  и ключ кладёт владелец командой `python -m harvester.web` (`__main__`);
* **без `SUDRF_WEB_PASSWORD_HASH` защиты нет** — это локальный режим по
  `ssh -L`. Публичный unit ставит `SUDRF_WEB_REQUIRE_AUTH=1`, и тогда
  сервис без хеша не запускается, а не открывается всем.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

COOKIE = "sudrf_session"
SESSION_DAYS = 14

#: Сколько неудачных входов с одного адреса подряд и на какое окно.
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024}


def hash_password(password: str) -> str:
    """`scrypt:<n>:<r>:<p>:<соль hex>:<хеш hex>` — без `$` и кавычек, чтобы
    значение безопасно лежало в `EnvironmentFile` systemd."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt:{_SCRYPT['n']}:{_SCRYPT['r']}:{_SCRYPT['p']}:{salt.hex()}:{digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split(":")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            dklen=len(digest) // 2,
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT["maxmem"],
        )
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


@dataclass
class Auth:
    password_hash: str
    session_key: str
    #: Cookie только по HTTPS. Выключается лишь для проверки на `http://localhost`.
    secure_cookie: bool = True
    _failures: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- cookie ---------------------------------------------------------

    def _sign(self, payload: str) -> str:
        return hmac.new(self.session_key.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def make_cookie(self, now: float | None = None) -> str:
        expires = str(int((now or time.time()) + SESSION_DAYS * 86400))
        return f"{expires}.{self._sign(expires)}"

    def valid_cookie(self, value: str | None, now: float | None = None) -> bool:
        if not value:
            return False
        expires, _, signature = value.partition(".")
        if not expires.isdigit() or not hmac.compare_digest(signature, self._sign(expires)):
            return False
        return int(expires) > (now or time.time())

    # --- перебор --------------------------------------------------------

    def blocked(self, client: str, now: float | None = None) -> bool:
        now = now or time.time()
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW]
            self._failures[client] = recent
            return len(recent) >= MAX_FAILURES

    def failed(self, client: str, now: float | None = None) -> None:
        with self._lock:
            self._failures.setdefault(client, []).append(now or time.time())

    def forgive(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


def from_env(environ=os.environ) -> Auth | None:
    """Защита из окружения. `None` — локальный режим без пароля."""
    stored = environ.get("SUDRF_WEB_PASSWORD_HASH", "").strip()
    key = environ.get("SUDRF_WEB_SESSION_KEY", "").strip()
    if not stored:
        if environ.get("SUDRF_WEB_REQUIRE_AUTH") == "1":
            raise RuntimeError("SUDRF_WEB_REQUIRE_AUTH=1, но SUDRF_WEB_PASSWORD_HASH не задан")
        return None
    if len(key) < 32:
        raise RuntimeError("SUDRF_WEB_SESSION_KEY короче 32 знаков — это не ключ")
    return Auth(stored, key, secure_cookie=environ.get("SUDRF_WEB_INSECURE_COOKIE") != "1")


LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Вход — судебная практика</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font:16px/1.4 "PT Sans",system-ui,sans-serif;color:#1B2430;background:#fff}}
form{{width:100%;max-width:380px;padding:24px;display:flex;flex-direction:column;gap:16px}}
h1{{margin:0;font:700 28px "PT Serif",Georgia,serif;color:#16335A;text-align:center}}
input{{height:52px;border:1px solid #8794A6;border-radius:26px;padding:0 20px;
font:inherit;font-size:18px}}
button{{height:52px;border:0;border-radius:26px;background:#1F4E8C;color:#fff;
font:inherit;font-weight:700;cursor:pointer}}
:focus-visible{{outline:2px solid #1F4E8C;outline-offset:2px}}
.err{{color:#8A2418;text-align:center;font-size:15px}}
</style></head><body>
<form method="post" action="/login">
<h1>Судебная практика</h1>
<label for="p" style="position:absolute;left:-9999px">Пароль</label>
<input id="p" name="password" type="password" autocomplete="current-password"
placeholder="Пароль" autofocus required>
{message}
<button type="submit">Войти</button>
</form></body></html>"""
