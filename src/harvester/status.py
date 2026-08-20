"""Состояние сбора одним взглядом.

`harvester queue` отвечает на вопрос «сколько окон осталось», и этого
хватало, пока прогон шёл по ночам и итог подводился утром. Сбор идёт
неделю подряд, и вопросов стало больше: идёт ли он вообще, с каким темпом,
когда кончится, не сыплются ли отказы. Каждый из них — отдельный запрос
к базе, и писать их руками по нескольку раз в день значит не писать.

Всё считается по базе, без сети и без чтения логов: журнал `harvest_run`
знает и время, и объём, и статус каждого окна. Отказы платформы видны
там же — с 20.08 `429` приходит как `throttled`, а не как разбитое окно.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, func, select

from .db.schema import cartoteka_volume, case, harvest_run, harvest_task

#: Дел на странице выдачи. Из него считается, во сколько запросов
#: обойдётся остаток: строки мы знаем, а запросы — нет.
PAGE_SIZE = 25

#: Доля корпуса, которую даёт ось публикации. Ось поступления берёт всё,
#: публикация — только дела с опубликованным актом, и это примерно
#: на 7 % меньше (замер 5 КСОЮ, см. roadmap 3.1).
PUBLICATION_SHARE = 0.93


@dataclass(frozen=True, slots=True)
class Progress:
    windows_done: int
    windows_total: int
    rows_collected: int
    rows_expected: int
    #: Запросов в час за последний час. `None` — за час ничего не закрылось.
    rate_per_hour: float | None
    #: Когда закрылось последнее окно. `None` — не закрывалось ни одного.
    last_window_at: datetime | None
    complete_today: int
    throttled_today: int
    failed_today: int

    @property
    def windows_share(self) -> float:
        return self.windows_done / self.windows_total if self.windows_total else 0.0

    @property
    def rows_share(self) -> float:
        return self.rows_collected / self.rows_expected if self.rows_expected else 0.0

    @property
    def hours_left(self) -> float | None:
        """Сколько часов до конца сбора при нынешнем темпе.

        Считается по СТРОКАМ, а не по окнам: окна разного размера, и доля
        закрытых окон врёт тем сильнее, чем неравномернее суды. К утру
        20.08 был закрыт почти весь Военный суд, самый маленький из десяти,
        и оценка по окнам обещала полтора суток вместо шести.
        """
        if not self.rate_per_hour:
            return None
        rows_left = max(0, self.rows_expected - self.rows_collected)
        return (rows_left / PAGE_SIZE) / self.rate_per_hour


def collect(engine: Engine, *, now: datetime | None = None) -> Progress:
    """Снять состояние сбора."""
    with engine.connect() as connection:
        moment = now or connection.execute(select(func.now())).scalar_one()
        hour_ago = moment - timedelta(hours=1)
        day_ago = moment - timedelta(days=1)

        windows_total = connection.execute(
            select(func.count()).select_from(harvest_task)
        ).scalar_one()
        windows_done = connection.execute(
            select(func.count()).where(harvest_task.c.status == "done")
        ).scalar_one()
        rows_collected = connection.execute(select(func.count()).select_from(case)).scalar_one()

        corpus = (
            connection.execute(
                select(func.coalesce(func.sum(cartoteka_volume.c.total_cases), 0)).where(
                    cartoteka_volume.c.status == "measured"
                )
            ).scalar_one()
            or 0
        )

        rows_last_hour = (
            connection.execute(
                select(func.coalesce(func.sum(harvest_run.c.fetched_rows), 0)).where(
                    harvest_run.c.finished_at >= hour_ago
                )
            ).scalar_one()
            or 0
        )

        last_window_at = connection.execute(
            select(func.max(harvest_run.c.finished_at))
        ).scalar_one()

        by_status = dict(
            connection.execute(
                select(harvest_run.c.status, func.count())
                .where(harvest_run.c.started_at >= day_ago)
                .group_by(harvest_run.c.status)
            ).all()
        )

    return Progress(
        windows_done=windows_done,
        windows_total=windows_total,
        rows_collected=rows_collected,
        # Индекс идёт по двум осям: поступление берёт всё, публикация — почти всё.
        rows_expected=round(corpus * (1 + PUBLICATION_SHARE)),
        rate_per_hour=rows_last_hour / PAGE_SIZE if rows_last_hour else None,
        last_window_at=last_window_at,
        complete_today=by_status.get("complete", 0),
        throttled_today=by_status.get("throttled", 0),
        failed_today=by_status.get("failed", 0),
    )


def by_court(engine: Engine) -> list[tuple[str, int, int, int]]:
    """По каждому суду: закрыто окон, осталось, собрано дел."""
    with engine.connect() as connection:
        done = dict(
            connection.execute(
                select(harvest_task.c.court_domain, func.count())
                .where(harvest_task.c.status == "done")
                .group_by(harvest_task.c.court_domain)
            ).all()
        )
        total = dict(
            connection.execute(
                select(harvest_task.c.court_domain, func.count()).group_by(
                    harvest_task.c.court_domain
                )
            ).all()
        )
        cases = dict(
            connection.execute(
                select(case.c.court_domain, func.count()).group_by(case.c.court_domain)
            ).all()
        )

    rows = [
        (court, done.get(court, 0), total[court] - done.get(court, 0), cases.get(court, 0))
        for court in total
    ]
    return sorted(rows, key=lambda row: row[2], reverse=True)


def _thousands(number: float) -> str:
    """Разряды неразрывным пробелом: 189 999 читается, 189999 — нет."""
    return f"{round(number):,}".replace(",", "\u00a0")


def _percent(share: float) -> str:
    """Проценты по-русски: запятая и пробел перед знаком."""
    return f"{share * 100:.1f}".replace(".", ",") + "\u00a0%"


def _plural(number: int, one: str, few: str, many: str) -> str:
    """Русское согласование: 1 запрос, 2 запроса, 5 запросов."""
    tail_two, tail = number % 100, number % 10
    if 11 <= tail_two <= 14:
        return many
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _spell_hours(hours: float) -> str:
    if hours < 48:
        return f"{hours:.0f} ч"
    days = f"{hours / 24:.1f}".replace(".", ",")
    return f"{hours:.0f} ч ≈ {days} суток"


def render(progress: Progress, courts: list[tuple[str, int, int, int]], *, now: datetime) -> str:
    """Собрать отчёт для терминала."""
    lines: list[str] = []

    if progress.last_window_at is None:
        lines.append("Прогон: окон ещё не закрывалось")
    else:
        idle = (now - progress.last_window_at).total_seconds()
        # Окно живёт минутами: если за четверть часа не закрылось ни одного,
        # прогон либо стоит на паузе у всех судов, либо не запущен.
        state = "идёт" if idle < 900 else "МОЛЧИТ"
        lines.append(f"Прогон: {state}, последнее окно закрыто {idle / 60:.0f} мин назад")

    lines.append("")
    lines.append(
        f"Очередь   закрыто {_thousands(progress.windows_done)}"
        f" из {_thousands(progress.windows_total)}  ({_percent(progress.windows_share)})"
    )
    lines.append(
        f"Собрано   {_thousands(progress.rows_collected)} дел"
        f" из ≈ {_thousands(progress.rows_expected)} по двум осям"
        f"  ({_percent(progress.rows_share)})"
    )
    if progress.rate_per_hour:
        rate = round(progress.rate_per_hour)
        word = _plural(rate, "запрос", "запроса", "запросов")
        lines.append(f"Темп      {_thousands(rate)} {word} в час")
        hours_left = progress.hours_left
        if hours_left is not None:
            lines.append(f"Осталось  ≈ {_spell_hours(hours_left)}")
    else:
        lines.append("Темп      за последний час не закрыто ни одного окна")

    lines.append("")
    lines.append(
        f"За сутки  окон собрано {_thousands(progress.complete_today)}, "
        f"придержано {_thousands(progress.throttled_today)}, "
        f"не собралось {_thousands(progress.failed_today)}"
    )
    if progress.failed_today:
        lines.append("          неудачи стоит посмотреть: harvest_run со статусом failed")

    lines.append("")
    lines.append(f"{'Суд':16} {'закрыто':>8} {'осталось':>9} {'дел':>9}")
    for court, done, left, cases_found in courts:
        lines.append(
            f"{court:16} {_thousands(done):>8} {_thousands(left):>9} {_thousands(cases_found):>9}"
        )

    return "\n".join(lines)
