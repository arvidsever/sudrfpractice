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


@dataclass(frozen=True, slots=True)
class PullResult:
    downloaded: int
    skipped: int


def pull(store, raw_root: Path, *, limit: int | None = None, dry_run: bool = False) -> PullResult:
    """Забрать сырьё из архива на диск — обратная сторона `push`.

    Нужна не «на всякий случай»: сырьё дороже базы (база выводится из него
    переразбором за часы, а обход судов заново — недели), и до 28.08.2026
    в коде была только выгрузка. Копия, которую нечем развернуть, копией
    не считается.

    Скачивается только отсутствующее: страница адресуется своим sha256,
    поэтому «есть файл с таким именем» и значит «та самая страница».
    """
    downloaded = skipped = 0
    for key in sorted(store.list_keys(PREFIX_RAW)):
        if limit is not None and downloaded >= limit:
            break
        target = raw_root / key[len(PREFIX_RAW) :]
        if target.exists():
            skipped += 1
            continue
        if not dry_run:
            store.get_file(key, target)
        downloaded += 1

    log.info("скачано %d, уже было %d%s", downloaded, skipped, " (вхолостую)" if dry_run else "")
    return PullResult(downloaded=downloaded, skipped=skipped)


@dataclass(frozen=True, slots=True)
class PruneResult:
    removed: int
    kept: int
    bytes_freed: int


def _year_dirs(raw_root: Path) -> list[Path]:
    """Каталоги `<домен>/<год>`, от старого года к новому.

    Год — единица чистки, а не отдельный файл: так и список ключей
    из бакета остаётся маленьким (одна выдача на каталог вместо миллиона
    ключей в памяти), и локально остаётся свежее, которое вероятнее
    понадобится для переразбора.
    """
    dirs = [
        year
        for domain in raw_root.iterdir()
        if domain.is_dir()
        for year in domain.iterdir()
        if year.is_dir()
    ]
    return sorted(dirs, key=lambda path: (path.name, path.parent.name))


def prune(
    store,
    raw_root: Path,
    *,
    keep_free: float = 0.5,
    dry_run: bool = False,
) -> PruneResult:
    """Освободить диск, удалив страницы, подтверждённые в бакете.

    Условие записано заранее, ещё до переезда (`docs/storage.md`):
    как только занято больше половины диска — удалять локальные страницы,
    **подтверждённые запросом к бакету**, и никогда — записью в журнале.
    Журнал говорит, что выгрузка прошла; 11.09.2026 он говорил это
    четверо суток подряд, пока дамп не выгружался вовсе.

    Поэтому подтверждение здесь ровно одно: ключ пришёл из выдачи
    хранилища. Не нашёлся — файл остаётся лежать, сколько бы места
    ни требовалось. Сырьё дороже базы: база выводится из него переразбором
    за часы, а обход судов заново — недели.

    Чистка идёт от старых лет к новым и останавливается, как только
    свободного места стало достаточно.
    """
    import shutil

    total, _, free = shutil.disk_usage(raw_root)
    need = int(total * keep_free) - free
    if need <= 0:
        log.info("свободно %.0f %% — чистить нечего", 100 * free / total)
        return PruneResult(removed=0, kept=0, bytes_freed=0)

    removed = kept = freed = 0
    for directory in _year_dirs(raw_root):
        prefix = PREFIX_RAW + directory.relative_to(raw_root).as_posix() + "/"
        confirmed = set(store.list_keys(prefix))
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix not in (".zst", ".json"):
                continue
            if PREFIX_RAW + path.relative_to(raw_root).as_posix() not in confirmed:
                kept += 1
                continue
            size = path.stat().st_size
            if not dry_run:
                path.unlink()
            removed += 1
            freed += size
            if freed >= need:
                break
        if freed >= need:
            break

    log.info(
        "удалено %d, освобождено %.1f ГБ, оставлено неподтверждённых %d%s",
        removed,
        freed / 2**30,
        kept,
        " (вхолостую)" if dry_run else "",
    )
    return PruneResult(removed=removed, kept=kept, bytes_freed=freed)


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
