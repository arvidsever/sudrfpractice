"""Выгрузка в объектное хранилище — без сети.

Проверяется решение «что и куда», а не разговор с бакетом: логика
разницы и раскладка ключей отделены от boto3 намеренно.
"""

from __future__ import annotations

import json
from pathlib import Path

from harvester.archive import (
    PREFIX_MODEL,
    PREFIX_RAW,
    model_upload,
    push,
    raw_uploads,
)


class _Store:
    def __init__(self, existing: set[str] | None = None):
        self.existing = existing or set()
        self.put: list[str] = []

    def list_keys(self, prefix: str):
        return [k for k in self.existing if k.startswith(prefix)]

    def put_file(self, key: str, path: Path) -> None:
        self.put.append(key)

    def get_file(self, key: str, path: Path) -> None:  # pragma: no cover
        raise NotImplementedError


def _raw(tmp_path: Path) -> Path:
    root = tmp_path / "raw"
    for domain, year, sha in (
        ("2kas.sudrf.ru", "2026", "ab" + "0" * 62),
        ("5kas.sudrf.ru", "2026", "cd" + "1" * 62),
    ):
        folder = root / domain / year / sha[:2]
        folder.mkdir(parents=True)
        (folder / f"{sha}.html.zst").write_bytes(b"x" * 100)
        (folder / f"{sha}.json").write_text('{"url": "…"}', encoding="utf-8")
    return root


def test_raw_keys_repeat_the_layout_on_disk(tmp_path: Path) -> None:
    keys = {u.key for u in raw_uploads(_raw(tmp_path))}
    assert keys == {
        f"{PREFIX_RAW}2kas.sudrf.ru/2026/ab/ab{'0' * 62}.html.zst",
        f"{PREFIX_RAW}2kas.sudrf.ru/2026/ab/ab{'0' * 62}.json",
        f"{PREFIX_RAW}5kas.sudrf.ru/2026/cd/cd{'1' * 62}.html.zst",
        f"{PREFIX_RAW}5kas.sudrf.ru/2026/cd/cd{'1' * 62}.json",
    }


def test_provenance_travels_with_the_page(tmp_path: Path) -> None:
    """Без json-спутника архив превращается в мешок неопознанных страниц."""
    uploads = list(raw_uploads(_raw(tmp_path)))
    assert sum(1 for u in uploads if u.key.endswith(".json")) == 2
    assert sum(1 for u in uploads if u.key.endswith(".zst")) == 2


def test_already_stored_is_not_sent_again(tmp_path: Path) -> None:
    """Сырьё адресуется содержимым, значит лежащее в бакете не меняется.
    Перезаливать сорок гигабайт мелких файлов на каждый прогон нельзя."""
    uploads = list(raw_uploads(_raw(tmp_path)))
    store = _Store(existing={uploads[0].key, uploads[1].key})

    result = push(store, uploads)

    assert (result.uploaded, result.skipped) == (2, 2)
    assert set(store.put) == {u.key for u in uploads[2:]}


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    store = _Store()
    result = push(store, raw_uploads(_raw(tmp_path)), dry_run=True)

    assert result.uploaded == 4
    assert store.put == []


def test_missing_raw_directory_is_not_an_error(tmp_path: Path) -> None:
    assert list(raw_uploads(tmp_path / "нет")) == []


def test_model_name_carries_the_training_date(tmp_path: Path) -> None:
    """Старые веса не затираются: по имени видно, когда обучены."""
    path = tmp_path / "captcha-model.json"
    path.write_text(json.dumps({"ts": "2026-08-19T18:12:00Z", "w1": []}), encoding="utf-8")

    upload = model_upload(path)
    assert upload is not None
    assert upload.key == f"{PREFIX_MODEL}captcha-model-2026-08-19.json"


def test_broken_model_file_still_uploads(tmp_path: Path) -> None:
    """Испорченный файл не повод падать при выгрузке: имя будет хуже,
    но веса уедут."""
    path = tmp_path / "captcha-model.json"
    path.write_text("не json", encoding="utf-8")

    upload = model_upload(path)
    assert upload is not None
    assert upload.key.endswith("неизвестно.json")


def test_no_model_no_upload(tmp_path: Path) -> None:
    assert model_upload(tmp_path / "нет.json") is None
