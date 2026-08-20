"""S3-совместимое хранилище: тонкая обёртка над boto3.

Отдельно от `archive.py` намеренно: там логика «что и куда выгружать»,
и она проверяется тестами без сети; здесь — только разговор с бакетом.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from .config import settings

log = logging.getLogger("harvester.s3")


class S3Store:
    """Клиент бакета. Ключи и секреты — только из окружения."""

    def __init__(
        self,
        bucket: str | None = None,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ):
        import boto3

        self.bucket = bucket or settings.s3_bucket
        if not self.bucket:
            raise ValueError("не задан бакет: HARVESTER_S3_BUCKET")

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or settings.s3_endpoint_url or None,
            region_name=settings.s3_region or None,
            aws_access_key_id=access_key or settings.s3_access_key or None,
            aws_secret_access_key=secret_key or settings.s3_secret_key or None,
        )

    def list_keys(self, prefix: str) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"]

    def put_file(self, key: str, path: Path) -> None:
        self._client.upload_file(str(path), self.bucket, key)

    def delete(self, key: str) -> None:
        """Удалить объект. Нужен только для ротации дампов: сырьё
        не удаляется никогда — оно адресуется содержимым и дороже базы."""
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def get_file(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, key, str(path))
