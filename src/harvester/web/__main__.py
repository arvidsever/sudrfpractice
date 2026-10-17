"""`python -m harvester.web` — секреты веб-интерфейса.

Без аргументов — хеш пароля и ключ сессии для `/etc/sudrf/web.env`.
`tokens …` — токены приложения Sudrf (sudrfpractice#5):

    python -m harvester.web tokens issue <имя>        # ручной токен владельца
    python -m harvester.web tokens list
    python -m harvester.web tokens revoke <id>
    python -m harvester.web tokens revoke-holder <держатель>

Хранилище — `SUDRF_WEB_TOKENS_PATH` или `--store`. На сервере запускать от
пользователя сервиса: `runuser -u sudrf -- …` (sudo на сервере нет), иначе файл станет чужим.
Пароль и токены печатаются только в терминале владельца и в чат,
репозиторий и журналы не попадают.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from datetime import datetime

from .auth import hash_password
from .tokens import Store


def _password() -> int:
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


def _when(value: float | None) -> str:
    return datetime.fromtimestamp(value).strftime("%d.%m.%Y %H:%M") if value else "—"


def _tokens(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m harvester.web tokens")
    parser.add_argument(
        "--store",
        default=os.environ.get("SUDRF_WEB_TOKENS_PATH"),
        help="файл хранилища, по умолчанию SUDRF_WEB_TOKENS_PATH",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", help="выпустить ручной токен владельца")
    issue.add_argument("name", help="чей токен, например «owner» или «macbook»")
    commands.add_parser("list", help="все токены и регистрации")
    revoke = commands.add_parser("revoke", help="отозвать один токен")
    revoke.add_argument("id", type=int)
    holder = commands.add_parser("revoke-holder", help="отозвать все токены держателя")
    holder.add_argument("holder")
    args = parser.parse_args(argv)
    if not args.store:
        print("не задан файл хранилища: --store или SUDRF_WEB_TOKENS_PATH", file=sys.stderr)
        return 1

    store = Store(args.store)
    try:
        if args.command == "issue":
            # Печатается один раз: в хранилище остаётся только хеш.
            print(store.issue("owner", args.name))
        elif args.command == "list":
            for t in store.list():
                state = f"отозван {_when(t.revoked)}" if t.revoked else "действует"
                used = f"{t.used_count} за {t.used_day}" if t.used_day else "не использовался"
                print(
                    f"{t.id:>4}  {t.kind:<7}  {t.holder:<24}  {t.environment or '—':<10}"
                    f"  выдан {_when(t.created)}  {state}  {used}"
                )
        elif args.command == "revoke":
            if not store.revoke(args.id):
                print("такого действующего токена нет", file=sys.stderr)
                return 1
        elif args.command == "revoke-holder":
            print(f"отозвано: {store.revoke_holder(args.holder)}")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "tokens":
        return _tokens(argv[1:])
    return _password()


if __name__ == "__main__":
    raise SystemExit(main())
