"""Токены приложения Sudrf (sudrfpractice#5).

Два вида:

* **установка** — выдаётся `POST /api/register` по проверенной записи
  App Store (`appstore.py`); держатель — `appTransactionId`, постоянный для
  пары «Apple ID + приложение». Не больше `MAX_PER_HOLDER` действующих
  токенов на держателя;
* **владелец** — выпускается командой `python -m harvester.web tokens issue`
  для сборок не из TestFlight.

Намеренные упрощения:

* **хранилище — SQLite из стандартной библиотеки**, отдельно от корпуса:
  соединение с корпусом только на чтение, а токены надо писать. Файл лежит
  в `StateDirectory` сервиса (`/var/lib/sudrf-web`), 600;
* **в файле только SHA-256 токена.** Токен — 32 случайных байта, перебирать
  его бессмысленно, медленный хеш вроде `scrypt` здесь не нужен;
* **лимит — в памяти процесса.** Сервис один; перезапуск сбрасывает окна,
  это безопасно.
* **слова запросов не пишутся**: на токен считается только число запросов
  за день.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path

#: Сколько действующих токенов допускается на одного держателя (тестера).
MAX_PER_HOLDER = 3

#: Лимит запросов на токен: в минуту и одновременно. Поиск по словам идёт
#: 7–12 секунд, на той же машине работают сборщики.
PER_MINUTE = 30
CONCURRENT = 2

_SCHEMA = """
create table if not exists token (
    id integer primary key,
    digest text not null unique,
    kind text not null check (kind in ('install', 'owner')),
    holder text not null,
    environment text,
    created real not null,
    revoked real,
    used_day text,
    used_count integer not null default 0
)
"""


@dataclass(frozen=True, slots=True)
class Token:
    id: int
    kind: str
    holder: str
    environment: str | None
    created: float
    revoked: float | None
    used_day: str | None
    used_count: int


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    """Токены в SQLite. Потокобезопасно: uvicorn зовёт из пула потоков."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("pragma journal_mode=wal")
        self._db.execute(_SCHEMA)
        if self.path.exists():
            self.path.chmod(0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def issue(self, kind: str, holder: str, environment: str | None = None) -> str:
        """Новый токен; возвращается один раз, в файле остаётся только хеш."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._db.execute(
                "insert into token (digest, kind, holder, environment, created)"
                " values (?, ?, ?, ?, ?)",
                (_digest(token), kind, holder, environment, time.time()),
            )
        return token

    def active_count(self, holder: str) -> int:
        with self._lock:
            row = self._db.execute(
                "select count(*) from token where holder = ? and revoked is null"
                " and kind = 'install'",
                (holder,),
            ).fetchone()
        return int(row[0])

    def check(self, token: str, today: date | None = None) -> Token | None:
        """Действующий токен или `None`. Попутно считает запрос за день."""
        day = (today or date.today()).isoformat()
        with self._lock:
            row = self._db.execute(
                "select id from token where digest = ? and revoked is null", (_digest(token),)
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "update token set used_count = case when used_day = ? then used_count + 1"
                " else 1 end, used_day = ? where id = ?",
                (day, day, row[0]),
            )
        return self.get(row[0])

    def get(self, token_id: int) -> Token | None:
        with self._lock:
            row = self._db.execute(
                "select id, kind, holder, environment, created, revoked, used_day, used_count"
                " from token where id = ?",
                (token_id,),
            ).fetchone()
        return Token(*row) if row else None

    def list(self) -> list[Token]:
        with self._lock:
            rows = self._db.execute(
                "select id, kind, holder, environment, created, revoked, used_day, used_count"
                " from token order by id"
            ).fetchall()
        return [Token(*row) for row in rows]

    def revoke(self, token_id: int) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "update token set revoked = ? where id = ? and revoked is null",
                (time.time(), token_id),
            )
        return cursor.rowcount > 0

    def revoke_holder(self, holder: str) -> int:
        with self._lock:
            cursor = self._db.execute(
                "update token set revoked = ? where holder = ? and revoked is null",
                (time.time(), holder),
            )
        return cursor.rowcount


class Limiter:
    """Окно в минуту и число одновременных запросов — на токен."""

    def __init__(self, per_minute: int = PER_MINUTE, concurrent: int = CONCURRENT) -> None:
        self.per_minute = per_minute
        self.concurrent = concurrent
        self._lock = threading.Lock()
        self._recent: dict[int, deque[float]] = {}
        self._running: dict[int, int] = {}

    def acquire(self, token_id: int, now: float | None = None) -> int | None:
        """`None` — можно; число — через сколько секунд повторить."""
        now = now or time.monotonic()
        with self._lock:
            window = self._recent.setdefault(token_id, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= self.per_minute:
                return max(1, int(60 - (now - window[0])) + 1)
            if self._running.get(token_id, 0) >= self.concurrent:
                return 1
            window.append(now)
            self._running[token_id] = self._running.get(token_id, 0) + 1
            return None

    def release(self, token_id: int) -> None:
        with self._lock:
            self._running[token_id] = max(0, self._running.get(token_id, 0) - 1)
