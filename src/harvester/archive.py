"""Выгрузка того, что не место в git, в объектное хранилище.

Разделение простое: код и метод живут в публичном репозитории, а собранные
данные и средства доступа — рядом с бэкапами, в S3-совместимом хранилище.
Причины разные и стоит их не путать:

* **сырьё** — размер (десятки гигабайт) и те же персональные данные,
  что в базе;
* **веса решателя капчи** — рабочий решатель капчи госпортала в открытом
  доступе публиковать не стоит;
* **дамп базы** — строка выдачи уголовной кассации и КоАП несёт ФИО
  вместе со вменяемой статьёй (`docs/ethics.md`).

Сырьё адресуется содержимым (`sha256`), поэтому выгрузка идёт разницей:
уже лежащее в хранилище не перезаливается. Это важно не ради трафика —
он у провайдера бесплатный, — а ради времени: сорок гигабайт мелких
файлов заливаются долго, и повторять это на каждый прогон нельзя.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger("harvester.archive")

#: Раскладка в бакете. Префиксы разделены, чтобы права можно было выдать
#: раздельно: сырьё читают часто, веса — почти никогда.
PREFIX_RAW = "raw/"
PREFIX_MODEL = "model/"


class ObjectStore(Protocol):
    """То немногое, что нам нужно от S3."""

    def list_keys(self, prefix: str) -> Iterable[str]: ...

    def put_file(self, key: str, path: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class Upload:
    key: str
    path: Path
    size: int


@dataclass(frozen=True, slots=True)
class PushResult:
    uploaded: int
    skipped: int
    bytes_sent: int


def raw_uploads(raw_root: Path) -> Iterator[Upload]:
    """Что из слоя сырья подлежит выгрузке.

    Ключ повторяет раскладку на диске: `<домен>/<год>/<xx>/<sha256>.html.zst`.
    Рядом лежит `.json` с провенансом — он тоже едет, иначе архив превращается
    в мешок неопознанных страниц.
    """
    if not raw_root.exists():
        return
    for path in sorted(raw_root.rglob("*")):
        if path.is_file() and path.suffix in (".zst", ".json"):
            yield Upload(
                key=PREFIX_RAW + path.relative_to(raw_root).as_posix(),
                path=path,
                size=path.stat().st_size,
            )


def push(store: ObjectStore, uploads: Iterable[Upload], *, dry_run: bool = False) -> PushResult:
    """Залить то, чего в хранилище ещё нет."""
    uploads = list(uploads)
    prefixes = {upload.key.split("/", 1)[0] + "/" for upload in uploads}
    present: set[str] = set()
    for prefix in prefixes:
        present.update(store.list_keys(prefix))

    uploaded = skipped = sent = 0
    for upload in uploads:
        if upload.key in present:
            skipped += 1
            continue
        if not dry_run:
            store.put_file(upload.key, upload.path)
        uploaded += 1
        sent += upload.size

    log.info(
        "выгружено %d, уже было %d, объём %.1f МБ%s",
        uploaded,
        skipped,
        sent / 2**20,
        " (вхолостую)" if dry_run else "",
    )
    return PushResult(uploaded=uploaded, skipped=skipped, bytes_sent=sent)


def model_upload(model_path: Path) -> Upload | None:
    """Веса решателя. Имя с отметкой обучения, чтобы старые не затирались."""
    if not model_path.exists():
        return None
    import contextlib
    import json

    stamp = "неизвестно"
    # Испорченный файл не повод падать при выгрузке: имя будет хуже,
    # но веса всё равно уедут в хранилище.
    with contextlib.suppress(Exception):
        stamp = str(json.loads(model_path.read_text(encoding="utf-8")).get("ts", stamp))[:10]
    return Upload(
        key=f"{PREFIX_MODEL}captcha-model-{stamp}.json",
        path=model_path,
        size=model_path.stat().st_size,
    )
