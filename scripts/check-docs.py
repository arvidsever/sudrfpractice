#!/usr/bin/env python
"""Сверка документов с кодом и базой.

Документы стареют молча: в них нет ни тестов, ни компилятора. За сутки
живого сбора 20.08.2026 в плане набралось восемь расхождений с фактами,
и половину я нашёл только потому, что владелец ткнул пальцем.

Проверяется четыре вещи, каждая — с источником истины:

* **настройки** — числа правил доступа в текстах против `config.py`;
* **ссылки** — каждый упомянутый файл существует;
* **команды** — каждый `harvester <cmd>` есть в CLI;
* **счётчики** — «N тестов», «N окон», «N дел» против базы и `pytest`.

Запуск: `python scripts/check-docs.py`. Ненулевой код возврата означает
расхождение. База нужна не всегда: без неё проверка счётчиков пропускается.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted(ROOT.glob("docs/*.md")) + [ROOT / "README.md", ROOT / "AGENTS.md"]

#: Правило доступа: как его пишут в текстах и где лежит истина.
#: Проверяем не «встречается ли число», а «нет ли ДРУГОГО числа рядом
#: с этими словами» — иначе проверка ловила бы исторические справки.
RULES = [
    # `[^\n]` намеренно пускает `|`: правила чаще всего стоят в таблице,
    # и число лежит в соседней колонке. Класс без `|` до него не доходил —
    # проверка молчала на подменённом значении.
    (
        "пауза на хост",
        r"(?:[Пп]ауза между запросами к одному хосту|дроссел\w+ на хост)"
        r"[^\n]{0,40}?(\d+(?:[.,]\d+)?)\s*с",
        "min_delay_seconds",
    ),
    (
        "общая пауза",
        r"(?:между любыми двумя запросами|[Оо]бщ\w+ дроссел\w+)[^\n]{0,40}?(\d+(?:[.,]\d+)?)\s*с",
        "global_min_delay_seconds",
    ),
    (
        "потолок в сутки",
        r"[Пп]отолок запросов в сутки на суд[^\n]{0,20}?(\d[\d\s ]*\d)",
        "daily_request_cap",
    ),
]


def settings_values() -> dict[str, float]:
    sys.path.insert(0, str(ROOT / "src"))
    from harvester.config import Settings

    s = Settings()
    return {
        "min_delay_seconds": s.min_delay_seconds,
        "global_min_delay_seconds": s.global_min_delay_seconds,
        "daily_request_cap": float(s.daily_request_cap),
    }


def check_settings(problems: list[str]) -> None:
    values = settings_values()
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for label, pattern, key in RULES:
            for found in re.findall(pattern, text):
                got = float(found.replace(",", ".").replace(" ", "").replace(" ", ""))
                if abs(got - values[key]) > 1e-9:
                    problems.append(
                        f"{doc.relative_to(ROOT)}: {label} = {found.strip()}, "
                        f"а в config.py {values[key]:g}"
                    )


def check_links(problems: list[str]) -> None:
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for target in re.findall(r"\]\((?!https?:)([^)#]+)", text):
            path = (doc.parent / target).resolve()
            if not path.exists():
                problems.append(f"{doc.relative_to(ROOT)}: ссылка в никуда — {target}")


def check_commands(problems: list[str]) -> None:
    help_text = subprocess.run(
        [sys.executable, "-m", "harvester", "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    ).stdout
    known = set(re.findall(r"^\s{4}(\w[\w-]*)", help_text, re.M))
    if not known:
        problems.append("не удалось получить список команд харвестера")
        return
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for cmd in re.findall(r"harvester\s+([a-z][a-z-]+)", text):
            if cmd not in known and cmd not in {"queue"}:
                problems.append(f"{doc.relative_to(ROOT)}: нет такой команды — harvester {cmd}")


def check_counts(problems: list[str]) -> None:
    """Счётчики сводок — против базы и против самих тестов.

    Проверяются только утверждения о ЦЕЛОМ: сколько всего окон в очереди,
    сколько пар замерено, сколько тестов. Числа-подмножества («917 закрыто»,
    «1 019 умерли») сюда не попадают намеренно: проверка, которая ловит
    любое число перед словом «окон», не находит ничего, кроме ложных тревог.
    """
    live: dict[str, int] = {}
    try:
        from sqlalchemy import create_engine
        from sqlalchemy import text as sql

        sys.path.insert(0, str(ROOT / "src"))
        from harvester.config import settings

        engine = create_engine(settings.database_url)
        with engine.connect() as c:
            live["окон"] = c.execute(sql("select count(*) from harvest_task")).scalar_one()
            live["пар"] = c.execute(
                sql("select count(*) from cartoteka_volume where status='measured'")
            ).scalar_one()
    except Exception as exc:  # noqa: BLE001 — без базы эта часть просто пропускается
        print(f"счётчики базы не проверены: {type(exc).__name__}: {exc}")

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    ).stdout
    # `pytest --collect-only -q` печатает счёт по файлам, а не одной строкой
    # «N tests collected», — складываем.
    per_file = [int(n) for n in re.findall(r"^tests/\S+:\s*(\d+)$", collected, re.M)]
    if per_file:
        live["тестов"] = sum(per_file)

    #: Утверждение о целом: слово-якорь и число рядом с ним.
    claims = [
        ("окон", r"[Оо]чередь индекса\D*?(\d[\d\s ]*)\s*окон"),
        ("окон", r"(\d[\d\s ]*)\s*окон[:,]?\s*(?:две оси|из них)"),
        # Только сводки: в тексте есть и «сначала 24 пары из 40» —
        # это история замера, а не расхождение.
        ("пар", r"Корпус замерен[^|\n]*?(?:все\s+)?(\d+)\s*пар"),
        ("пар", r"[Зз]амер закончен\D*?(\d+)\s*пар"),
        ("тестов", r"\|\s*Тесты\s*\|\s*(\d+)"),
        ("тестов", r"(\d+)\s*тест(?:ов|а)?,\s*CI"),
    ]
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for label, pattern in claims:
            if label not in live:
                continue
            for found_text in re.findall(pattern, text):
                got = int(found_text.replace(" ", "").replace(" ", "").strip())
                if got != live[label]:
                    problems.append(
                        f"{doc.relative_to(ROOT)}: {got} {label}, а на деле {live[label]}"
                    )


def main() -> int:
    problems: list[str] = []
    check_settings(problems)
    check_links(problems)
    check_commands(problems)
    check_counts(problems)

    if not problems:
        print("документы сходятся с кодом и базой")
        return 0
    print(f"расхождений: {len(problems)}\n")
    for problem in problems:
        print(f"  {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
