"""Письмо владельцу, когда задание падает второй раз подряд.

Оплачено 24–26.09.2026: провайдер молча сменил адрес хранилища, и выгрузка
42 часа падала каждые шесть часов. Узнали об этом только потому, что
владелец спросил «как успехи». Спроси он через неделю — неделю не было бы
копий, и ни одна проверка об этом не сказала бы.

Одна неудача письма не стоит: 22.09 минутный сбой DNS провайдера исправил
следующий же заход. Две подряд — это уже не случайность. После письма
о падении приходит и письмо о починке: иначе тревога остаётся открытой,
и понять, кончилась ли она, можно только снова спросив.

Вызывается из `ExecStopPost=` задания: systemd кладёт исход запуска
в `SERVICE_RESULT` (`success`, `exit-code`, `timeout`…), а состояние
между запусками живёт в `STATE_DIRECTORY`, который systemd создаёт сам.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import subprocess
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path

from .config import Settings
from .config import settings as default_settings

log = logging.getLogger("harvester.alert")

#: После скольких неудач подряд писать.
THRESHOLD = 2


def record(unit: str, result: str, state_dir: Path) -> str | None:
    """Учесть исход запуска. Вернуть тему письма, если писать пора."""
    failures = state_dir / f"{unit}.failures"
    alerted = state_dir / f"{unit}.alerted"

    if result == "success":
        failures.unlink(missing_ok=True)
        if alerted.exists():
            alerted.unlink()
            return f"{unit}: снова работает"
        return None

    count = int(failures.read_text()) + 1 if failures.exists() else 1
    failures.write_text(str(count))
    # Пишем один раз на всю полосу неудач, а не каждые шесть часов. Метка
    # «уже написали» ставится после отправки, а не до: не ушло письмо —
    # попробуем на следующей неудаче, а не замолчим навсегда.
    if count >= THRESHOLD and not alerted.exists():
        return f"{unit}: упало {count} раз подряд"
    return None


def handle(unit: str, result: str, state_dir: Path, send: Callable[[str, str], None]) -> str | None:
    """Учесть исход и, если пора, отправить письмо."""
    subject = record(unit, result, state_dir)
    if subject is None:
        return None
    send(subject, _body(unit, result))
    if result != "success":
        (state_dir / f"{unit}.alerted").touch()
    return subject


def _body(unit: str, result: str) -> str:
    lines = [
        f"Сервер: {socket.gethostname()}",
        f"Задание: {unit}",
        f"Исход последнего запуска: {result}",
        "",
    ]
    if result != "success":
        # Последние строки журнала — чтобы из письма было видно причину,
        # а не только факт. Не читаются — письмо уходит и без них.
        try:
            tail = subprocess.run(
                ["journalctl", "-u", unit, "-n", "15", "--no-pager", "-o", "cat"],
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            tail = ""
        if tail:
            lines += ["Конец журнала:", tail, ""]
    lines.append(f"Подробности: ssh на сервер, затем journalctl -u {unit} -n 100")
    return "\n".join(lines)


def smtp_sender(settings: Settings | None = None) -> Callable[[str, str], None]:
    settings = settings or default_settings

    def send(subject: str, body: str) -> None:
        if not (settings.smtp_user and settings.smtp_password and settings.alert_to):
            # Не настроено — не ошибка задания. Состояние при этом ведётся,
            # и первая же неудача после настройки напишет.
            raise RuntimeError("почта не настроена: HARVESTER_SMTP_* и HARVESTER_ALERT_TO")
        message = EmailMessage()
        message["Subject"] = f"[sudrf] {subject}"
        message["From"] = settings.smtp_user
        message["To"] = settings.alert_to
        message.set_content(body)
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
        log.info("письмо отправлено: %s", subject)

    return send
