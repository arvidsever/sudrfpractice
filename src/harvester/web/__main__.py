"""`python -m harvester.web` — хеш пароля и ключ сессии для `/etc/sudrf/web.env`.

Пароль вводится в терминале владельца и в чат, репозиторий и журналы не
попадает: печатаются только хеш и случайный ключ.
"""

from __future__ import annotations

import getpass
import secrets
import sys

from .auth import hash_password


def main() -> int:
    password = getpass.getpass("Пароль: ")
    if len(password) < 12:
        print("короче 12 знаков — это не пароль для интернета", file=sys.stderr)
        return 1
    if getpass.getpass("Ещё раз: ") != password:
        print("не совпало", file=sys.stderr)
        return 1
    print(f"SUDRF_WEB_PASSWORD_HASH={hash_password(password)}")
    print(f"SUDRF_WEB_SESSION_KEY={secrets.token_urlsafe(32)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
