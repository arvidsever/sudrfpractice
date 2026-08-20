"""Дамп базы в объектное хранилище.

Сырьё дороже базы, но база не бесплатна: переразбор 2,7 миллиона страниц
из сырья — это часы счёта и заново построенные индексы. А когда появятся
эмбеддинги, восстановление будет стоить и вовсе три недели: столько
считаются векторы.

Устроено так, чтобы задание можно было будить часто, а дамп снимался редко:

* **дамп за сегодня уже в бакете — выходим.** Поэтому шестичасовое
  задание выгрузки может звать эту команду каждый раз, а получится
  раз в сутки;
* **старые дампы удаляются по счёту, а не по возрасту.** «Держим семь
  штук» переживает недельный простой, «удаляем старше недели» — нет
  и оставляет ноль копий.

Персональные данные: в дампе лежат ФИО из строк уголовной кассации
и КоАП (см. `ethics.md`), поэтому место ему только в приватном бакете.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from .config import Settings
from .config import settings as default_settings

log = logging.getLogger("harvester.dump")

#: Префикс в бакете. Отдельный от `raw/`, потому что и содержимое,
#: и срок жизни у них разные.
PREFIX = "dump/"

#: Сколько дампов держать. Семь — это неделя ежедневных, чего хватает,
#: чтобы заметить порчу данных и откатиться до неё.
KEEP = 7


def _key(day: date) -> str:
    return f"{PREFIX}praktika-{day:%Y-%m-%d}.dump"


def _database_name(url: str) -> str:
    """Имя базы из строки подключения SQLAlchemy."""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


def make_dump(
    store,
    *,
    settings: Settings | None = None,
    today: date | None = None,
    keep: int = KEEP,
    force: bool = False,
) -> str | None:
    """Снять дамп и положить в бакет. `None` — сегодняшний уже есть."""
    settings = settings or default_settings
    day = today or date.today()
    key = _key(day)

    if not force and key in set(store.list_keys(PREFIX)):
        log.info("дамп за %s уже в бакете", day)
        return None

    database = _database_name(settings.database_url)
    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "praktika.dump"
        log.info("снимаю дамп базы %s", database)
        # -Fc: формат, который читает pg_restore выборочно, по таблицам.
        # Плоский SQL пришлось бы накатывать целиком.
        subprocess.run(
            ["pg_dump", "-Fc", "-Z6", database, "-f", str(path)],
            check=True,
            capture_output=True,
        )
        size_mb = path.stat().st_size / 1024**2
        log.info("дамп %.0f МБ, выгружаю в %s", size_mb, key)
        store.put_file(key, path)

    _prune(store, keep=keep)
    return key


def _prune(store, *, keep: int) -> list[str]:
    """Удалить лишние дампы, оставив `keep` свежайших."""
    keys = sorted(store.list_keys(PREFIX))
    extra = keys[:-keep] if keep > 0 else []
    for key in extra:
        log.info("удаляю старый дамп %s", key)
        store.delete(key)
    return extra
