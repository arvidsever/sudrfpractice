"""Дамп базы в хранилище.

База не бесплатна, хоть сырьё и дороже: переразбор 2,7 миллиона страниц —
это часы, а когда появятся эмбеддинги, восстановление будет стоить недели
счёта. Но и дампов не должно копиться без счёта: они несут ФИО и занимают
место, за которое платят помегабайтно.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from harvester.dump import _database_name, make_dump


class _Bucket:
    """Бакет в памяти: кладём, перечисляем, удаляем."""

    def __init__(self, keys: list[str] | None = None):
        self.keys = list(keys or [])
        self.deleted: list[str] = []
        self.dumped: list[str] = []

    def list_keys(self, prefix: str):
        return [k for k in self.keys if k.startswith(prefix)]

    def put_file(self, key: str, path: Path) -> None:
        assert path.exists(), "дамп должен существовать до выгрузки"
        self.keys.append(key)
        self.dumped.append(key)

    def delete(self, key: str) -> None:
        self.keys.remove(key)
        self.deleted.append(key)


@pytest.fixture
def no_pg_dump(monkeypatch, tmp_path):
    """Подменяет pg_dump: тест про логику ротации, а не про Postgres."""

    def fake_run(command, **kwargs):
        Path(command[command.index("-f") + 1]).write_bytes(b"dump")
        return None

    monkeypatch.setattr("harvester.dump.subprocess.run", fake_run)


def test_todays_dump_is_not_taken_twice(no_pg_dump) -> None:
    """Задание выгрузки будится каждые шесть часов, а дамп нужен раз в сутки.
    Проверка «сегодняшний уже есть» и делает частые побудки безопасными."""
    bucket = _Bucket(["dump/praktika-2026-08-20.dump"])

    assert make_dump(bucket, today=date(2026, 8, 20)) is None
    assert bucket.dumped == []


def test_force_takes_it_anyway(no_pg_dump) -> None:
    bucket = _Bucket(["dump/praktika-2026-08-20.dump"])

    assert make_dump(bucket, today=date(2026, 8, 20), force=True) is not None


def test_only_the_freshest_are_kept(no_pg_dump) -> None:
    """Держим по счёту, а не по возрасту.

    «Удалять старше недели» после недельного простоя оставило бы ноль
    копий — ровно тогда, когда они и нужны.
    """
    old = [f"dump/praktika-2026-08-{day:02d}.dump" for day in range(10, 19)]
    bucket = _Bucket(old)

    make_dump(bucket, today=date(2026, 8, 20), keep=3)

    assert sorted(bucket.list_keys("dump/")) == [
        "dump/praktika-2026-08-17.dump",
        "dump/praktika-2026-08-18.dump",
        "dump/praktika-2026-08-20.dump",
    ]


def test_raw_is_never_touched(no_pg_dump) -> None:
    """Ротация трогает только дампы: сырьё не удаляется никогда."""
    bucket = _Bucket(
        ["raw/aa/bb.html.zst"] + [f"dump/praktika-2026-08-{d:02d}.dump" for d in range(10, 19)]
    )

    make_dump(bucket, today=date(2026, 8, 20), keep=1)

    assert "raw/aa/bb.html.zst" in bucket.keys
    assert all(key.startswith("dump/") for key in bucket.deleted)


def test_database_name_survives_the_url() -> None:
    assert _database_name("postgresql+psycopg://localhost:5432/praktika") == "praktika"
    assert _database_name("postgresql+psycopg://u:p@host/praktika?sslmode=require") == "praktika"
