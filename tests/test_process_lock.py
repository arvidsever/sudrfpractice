"""Замок на машину: два обхода разом дали бы двойной темп на ГАС.

Общий дроссель считает время в переменной процесса, и пока обход был один,
этого хватало. С расписанием их стало три — прогон очереди каждые полчаса,
суточный добег и многодневный свод карточек. Любые два, сойдясь во времени,
выдержали бы каждый свои 1,5 с, а платформа увидела бы запрос каждые 0,75.
Ровно на таком превышении 20.08.2026 семь судов ответили 429 в одну минуту.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from harvester.config import Settings
from harvester.http import AlreadyHarvesting, claim_harvest_lock


def test_second_process_is_turned_away(tmp_path) -> None:
    """Замок держит между ПРОЦЕССАМИ, а не потоками: launchd поднимает
    отдельные процессы, и `threading.Lock` их не видит."""
    settings = Settings(raw_root=tmp_path / "raw")
    claim_harvest_lock(settings)

    program = textwrap.dedent(f"""
        from harvester.config import Settings
        from harvester.http import AlreadyHarvesting, claim_harvest_lock
        try:
            claim_harvest_lock(Settings(raw_root={str(tmp_path / "raw")!r}))
        except AlreadyHarvesting:
            raise SystemExit(7)
        raise SystemExit(0)
    """)
    second = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)

    assert second.returncode == 7, f"второй процесс прошёл: {second.stderr}"


def test_lock_dies_with_the_process(tmp_path) -> None:
    """После выхода замок свободен. Файл-флаг остался бы лежать и запирал
    бы сбор до тех пор, пока кто-нибудь про него не вспомнит; `flock`
    снимает ядро."""
    settings = Settings(raw_root=tmp_path / "raw")
    program = textwrap.dedent(f"""
        from harvester.config import Settings
        from harvester.http import claim_harvest_lock
        claim_harvest_lock(Settings(raw_root={str(tmp_path / "raw")!r}))
    """)
    subprocess.run([sys.executable, "-c", program], check=True)

    claim_harvest_lock(settings)  # не должно бросить


def test_taken_twice_in_one_process_is_an_error(tmp_path) -> None:
    settings = Settings(raw_root=tmp_path / "raw")
    claim_harvest_lock(settings)
    with pytest.raises(AlreadyHarvesting):
        claim_harvest_lock(settings)


def test_waiting_asks_the_kernel_to_queue_instead_of_refusing(tmp_path, monkeypatch) -> None:
    """Добег обязан отработать, а не умереть.

    Свод карточек держит замок почти непрерывно шесть суток; при отказе
    вместо ожидания суточный добег не выполнился бы ни разу, и свежая
    практика в индекс не попала бы. 29.08.2026 так и вышло — добег умер
    в 05:00 с «обход уже идёт». Правило — «на суды ходит один процесс»,
    а не «второй умирает».

    Проверяется ровно решение: с `wait=True` замок берётся без `LOCK_NB`,
    то есть ядро ставит процесс в очередь, а не отказывает.
    """
    import fcntl

    flags: list[int] = []
    monkeypatch.setattr(fcntl, "flock", lambda handle, how: flags.append(how))

    claim_harvest_lock(Settings(raw_root=tmp_path / "ждём"), wait=True)
    claim_harvest_lock(Settings(raw_root=tmp_path / "не-ждём"))

    waiting, refusing = flags
    assert not waiting & fcntl.LOCK_NB, "ожидание — это блокирующий flock"
    assert refusing & fcntl.LOCK_NB, "остальным ждать нечего: отказ и выход"
