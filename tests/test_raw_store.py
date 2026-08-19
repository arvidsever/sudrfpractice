"""Слой сырья: провенанс и дедупликация по содержимому."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from harvester.raw import RawStore


def test_saves_original_bytes_not_decoded_text(tmp_path: Path) -> None:
    """Держим именно байты cp1251: перекодировка теряет то, чем можно будет
    объяснить будущую ошибку разбора."""
    store = RawStore(tmp_path)
    content = "<html>Найти</html>".encode("cp1251")

    record = store.save(
        content,
        url="https://2kas.sudrf.ru/modules.php?name_op=r",
        court_domain="2kas.sudrf.ru",
        http_status=200,
        content_kind="listing",
        fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
    )

    assert store.load(record) == content
    assert record.byte_size == len(content)
    assert record.path.startswith("2kas.sudrf.ru/2026/")


def test_same_content_lands_in_one_file(tmp_path: Path) -> None:
    """Имя от содержимого, а не от URL: один документ, пришедший разными
    путями, не задваивается."""
    store = RawStore(tmp_path)
    content = b"<html>same</html>"

    first = store.save(
        content,
        url="https://2kas.sudrf.ru/a",
        court_domain="2kas.sudrf.ru",
        http_status=200,
        content_kind="act",
    )
    second = store.save(
        content,
        url="https://2kas.sudrf.ru/b",
        court_domain="2kas.sudrf.ru",
        http_status=200,
        content_kind="act",
    )

    assert first.sha256 == second.sha256
    assert len(list(tmp_path.rglob("*.html.zst"))) == 1
