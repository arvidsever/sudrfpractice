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


def test_restore_brings_back_only_what_is_missing(tmp_path: Path) -> None:
    """Копия, которую нечем развернуть, копией не считается.

    Сырьё дороже базы: база выводится из него переразбором за часы, а обход
    судов заново — недели. До 28.08.2026 в коде была только выгрузка, и
    проверить архив было нечем.
    """
    from harvester.archive import pull

    sha = "ef" + "2" * 62
    key = f"{PREFIX_RAW}9kas.sudrf.ru/2026/{sha[:2]}/{sha}.html.zst"
    store = _RestoreStore({key: "страница".encode()})

    root = tmp_path / "raw"
    first = pull(store, root)
    assert (first.downloaded, first.skipped) == (1, 0)
    assert (
        root / f"9kas.sudrf.ru/2026/{sha[:2]}/{sha}.html.zst"
    ).read_bytes() == "страница".encode()

    # Второй заход ничего не качает: страница адресуется своим sha256,
    # поэтому «файл с таким именем есть» и значит «та самая страница».
    again = pull(store, root)
    assert (again.downloaded, again.skipped) == (0, 1)


class _RestoreStore(_Store):
    def __init__(self, objects: dict[str, bytes]):
        super().__init__(set(objects))
        self.objects = objects

    def get_file(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.objects[key])


def _disk(monkeypatch, *, total: int, free: int) -> None:
    """Подменить показания диска: prune смотрит на них, а не на файлы."""
    import collections

    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda _: usage(total, total - free, free))


def test_unconfirmed_pages_are_never_deleted(tmp_path, monkeypatch) -> None:
    """Подтверждение — только выдача бакета, и ничего кроме.

    Соблазн считать подтверждением журнал («выгружено 23 690») стоил бы
    корпуса: 11.09.2026 такая запись появлялась четверо суток подряд,
    а дамп при этом не выгружался вовсе — падал следующий шаг. Страница,
    которой нет в хранилище, остаётся на диске, сколько бы места
    ни требовалось: обход судов заново — это недели.
    """
    from harvester.archive import prune

    raw = tmp_path / "raw"
    (raw / "1kas.sudrf.ru" / "2025" / "ab").mkdir(parents=True)
    есть = raw / "1kas.sudrf.ru" / "2025" / "ab" / "подтверждённая.html.zst"
    нет = raw / "1kas.sudrf.ru" / "2025" / "ab" / "своя.html.zst"
    есть.write_bytes(b"x" * 100)
    нет.write_bytes(b"y" * 100)

    class _Bucket:
        def list_keys(self, prefix):
            key = "raw/1kas.sudrf.ru/2025/ab/подтверждённая.html.zst"
            return [key] if key.startswith(prefix) else []

    _disk(monkeypatch, total=1000, free=100)  # свободно 10 % при пороге 50 %

    result = prune(_Bucket(), raw)

    assert not есть.exists(), "подтверждённая страница должна быть удалена"
    assert нет.exists(), "неподтверждённую удалять нельзя ни при каком дефиците места"
    assert (result.removed, result.kept) == (1, 1)


def test_prune_does_nothing_while_there_is_room(tmp_path, monkeypatch) -> None:
    """Порог — это условие, а не расписание: пока места хватает, файлы лежат."""
    from harvester.archive import prune

    raw = tmp_path / "raw"
    (raw / "1kas.sudrf.ru" / "2025" / "ab").mkdir(parents=True)
    страница = raw / "1kas.sudrf.ru" / "2025" / "ab" / "стр.html.zst"
    страница.write_bytes(b"x" * 100)

    class _Bucket:
        def list_keys(self, prefix):
            return ["raw/1kas.sudrf.ru/2025/ab/стр.html.zst"]

    _disk(monkeypatch, total=1000, free=900)  # свободно 90 %

    assert prune(_Bucket(), raw).removed == 0
    assert страница.exists()
