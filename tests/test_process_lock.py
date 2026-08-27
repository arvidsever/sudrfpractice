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
