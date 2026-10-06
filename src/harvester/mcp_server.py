"""MCP-сервер: поиск по корпусу для Claude вне этого мака (docs/mcp.md).

Скилл `sudrf-practice` ходил на сервер по `ssh` с ключом владельца и потому
работал только в Claude Code на его маке. Здесь те же две команды — `search`
и `act` — отданы по MCP поверх HTTPS: их видит Cowork, веб и телефон.

Намеренные упрощения:

* инструменты вызывают сам CLI и возвращают его вывод. Формат выдачи уже
  выверен под чтение моделью, и второго, расходящегося с ним, не появляется;
* режимов два: на сервере — HTTP (`app`), на маке владельца — stdio с походом
  в корпус по `ssh` (`SUDRF_MCP_SSH`), без единого открытого порта;
* доступ по HTTP — секретом в пути адреса, а не OAuth: коннектор claude.ai умеет
  либо OAuth, либо ничего, а пользователь у сервера один. Секрет лежит
  в `/etc/sudrf/mcp.env`, в журнал и в репозиторий не попадает;
* только чтение: задание запускается с `default_transaction_read_only=on`
  и `statement_timeout` (deploy/systemd/sudrf-mcp.service) — эти две строки,
  а не аккуратность кода, не дают запросу снаружи ни писать, ни висеть.
"""

from __future__ import annotations

import contextlib
import io
import os
import threading

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

MAX_ROWS = 50
MAX_ACTS = 3

server = MCPServer(
    "sudrf-practice",
    instructions=(
        "Собственный корпус судебной практики: кассационные и апелляционные суды общей "
        "юрисдикции, Верховный Суд РФ (без экономической коллегии), Конституционный Суд РФ. "
        "search ищет по словам в тексте акта и по реквизитам, act отдаёт текст акта целиком. "
        "Цитировать можно только то, что прочитано через act. «Найдено 0» не значит, что "
        "практики нет: выдача сама пишет, у какой доли актов разобран текст."
    ),
)

# ponytail: один замок на все вызовы — CLI пишет в общий stdout, и два
# одновременных запроса перемешали бы выдачу. Пользователь один; появятся
# очереди — вынести печать из `__main__` в функции, возвращающие строку.
_STDOUT = threading.Lock()


def _over_ssh(target: str, argv: tuple[str, ...]) -> str:
    """Локальный режим: сервер MCP живёт на маке владельца и ходит в корпус
    по тому же `ssh`, что и скилл. Наружу ничего не открывается."""
    import shlex
    import subprocess

    inner = "cd /srv/sudrfpractice && .venv/bin/python -m harvester " + shlex.join(argv)
    try:
        done = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", target]
            + ["su sudrf -c " + shlex.quote(inner)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "корпус недоступен отсюда: сервер не ответил за две минуты"
    # 255 — код самого ssh: не дошли до сервера. Это не «ничего не нашлось»,
    # и путать их нельзя: на пустом ответе строят вывод «практики нет».
    if done.returncode == 255:
        return f"корпус недоступен отсюда: {done.stderr.strip()[-300:]}"
    return done.stdout or done.stderr or "запрос выполнен, но вывод пуст"


def _cli(*argv: str) -> str:
    if target := os.environ.get("SUDRF_MCP_SSH"):
        return _over_ssh(target, argv)

    from .__main__ import main

    out = io.StringIO()
    with _STDOUT, contextlib.redirect_stdout(out):
        try:
            main(list(argv))
        except SystemExit:
            # argparse на кривом аргументе выходит из процесса — сервер
            # от этого падать не должен.
            return "запрос не выполнен: неверные параметры"
    return out.getvalue() or "запрос выполнен, но вывод пуст"


@server.tool()
def search(
    text: str | None = None,
    number: str | None = None,
    court: list[str] | None = None,
    cartoteka: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> str:
    """Искать дела и акты в корпусе. Выдача — от свежих к старым, не по релевантности.

    text — слова в тексте акта: "фраза целиком" в кавычках, -слово исключает,
    or даёт альтернативу. Ищи термином, а не номером статьи.
    number — часть номера дела.
    court — домены судов: 1kas…9kas.sudrf.ru, vkas.sudrf.ru, 1ap…5ap.sudrf.ru,
    vap.sudrf.ru, www.vsrf.ru (Верховный Суд), www.ksrf.ru (Конституционный Суд).
    cartoteka — g3 гражданская, u3 уголовная, p3 КоАП, adm3 КАС (кассация);
    g2, u2, p2 (апелляция). С картотекой ВС и КС в выдачу не попадают.
    date_from, date_to — дата решения, дд.мм.гггг.
    """
    if not (text or number):
        return "нужен text или number: без них это не поиск, а листание всего корпуса"
    argv = ["search", "--limit", str(max(1, min(limit, MAX_ROWS))), "--offset", str(max(0, offset))]
    for flag, value in (
        ("--text", text),
        ("--number", number),
        ("--from", date_from),
        ("--to", date_to),
    ):
        if value:
            argv += [flag, value]
    for flag, values in (("--court", court), ("--cartoteka", cartoteka)):
        for value in values or ():
            argv += [flag, value]
    try:
        return _cli(*argv)
    except ValueError:
        return "запрос не выполнен: дата должна быть в виде дд.мм.гггг"


@server.tool()
def act(number: str, limit: int = 1) -> str:
    """Текст акта целиком по номеру дела из выдачи search (например 88-17060/2026,
    5-КГ26-1-К2, 57-П/2026). Читай акт перед тем, как на него сослаться."""
    return _cli("act", number, "--limit", str(max(1, min(limit, MAX_ACTS))))


def app():
    """ASGI-приложение для uvicorn (`--factory`). Секрет и имя хоста —
    из окружения: без секрета сервер не поднимается вовсе."""
    secret = os.environ["SUDRF_MCP_SECRET"]
    if len(secret) < 32:
        raise RuntimeError("SUDRF_MCP_SECRET короче 32 знаков — это не секрет")
    host = os.environ["SUDRF_MCP_HOST"]
    return server.streamable_http_app(
        streamable_http_path=f"/{secret}/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=[host]),
    )


if __name__ == "__main__":
    # Локальный коннектор Claude Desktop: stdio, корпус — по ssh
    # (`SUDRF_MCP_SSH=root@157.22.194.96`). См. docs/mcp.md.
    server.run()
