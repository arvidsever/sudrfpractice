"""Слой сырых HTML — источник истины.

Парсеры заведомо будут дорабатываться: платформа меняет вёрстку, а суды
отличаются друг от друга. Архив сырья избавляет от повторного обхода судов
при каждой такой доработке — это дешевле для нас и уважительнее к судам.

Раскладка: `raw/<домен>/<год>/<sha256[:2]>/<sha256>.html.zst`, рядом
`<sha256>.json` с провенансом. Имя от содержимого, а не от URL: один и тот же
документ, пришедший разными путями, не задваивается.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import zstandard

COMPRESSION_LEVEL = 10


@dataclass(frozen=True, slots=True)
class RawRecord:
    """Провенанс одной сохранённой страницы."""

    sha256: str
    url: str
    court_domain: str
    fetched_at: str
    http_status: int
    byte_size: int
    content_kind: str
    path: str


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, court_domain: str, year: int, sha256: str) -> Path:
        return self.root / court_domain / str(year) / sha256[:2] / f"{sha256}.html.zst"

    def save(
        self,
        content: bytes,
        *,
        url: str,
        court_domain: str,
        http_status: int,
        content_kind: str,
        fetched_at: datetime | None = None,
    ) -> RawRecord:
        """Сохранить страницу как есть — в исходных байтах, до перекодировки.

        Держим именно байты: перекодировка cp1251→UTF-8 теряет то, чем можно
        будет объяснить будущую ошибку разбора.
        """
        moment = fetched_at or datetime.now(UTC)
        sha256 = _digest(content)
        target = self.path_for(court_domain, moment.year, sha256)
        target.parent.mkdir(parents=True, exist_ok=True)

        record = RawRecord(
            sha256=sha256,
            url=url,
            court_domain=court_domain,
            fetched_at=moment.isoformat(),
            http_status=http_status,
            byte_size=len(content),
            content_kind=content_kind,
            path=str(target.relative_to(self.root)),
        )

        if not target.exists():
            compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL)
            target.write_bytes(compressor.compress(content))
        target.with_suffix(".json").write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return record

    def load(self, record: RawRecord) -> bytes:
        blob = (self.root / record.path).read_bytes()
        return zstandard.ZstdDecompressor().decompress(blob)
