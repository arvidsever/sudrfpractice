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

from sqlalchemy import Engine, and_, func, or_, select

from .db.schema import cartoteka_volume, case, harvest_run, harvest_task

#: Дел на странице выдачи. Из него считается, во сколько запросов
#: обойдётся остаток: строки мы знаем, а запросы — нет.
PAGE_SIZE = 25

#: Во сколько раз запросов больше, чем дел в базе. Индекс идёт по двум осям,
#: и вторая ось почти целиком повторяет первую: ось поступления берёт все
#: дела, публикация — только с опубликованным актом, примерно на 7 % меньше
#: (замер 5 КСОЮ, см. roadmap 3.1). В базу эти строки ложатся ОДНИМ делом —
#: `uq_case_court_uid`, — поэтому удваивать надо работу, а не ожидаемый корпус.
AXES_FACTOR = 1.93


def _confirmed_empty():
    """Пустое окно, у которого есть контроль: §5 грамматики, но бесплатно.

    Портал отвечает `200 OK` и на кривой запрос, поэтому «данных
    не обнаружено» само по себе не значит ничего: пустота и опечатка
    в запросе неразличимы. Разрешает их контрольный запрос с заведомо
    непустым окном — и его не надо делать заново, если рядом уже собрано
    окно ТОЙ ЖЕ формы (суд, картотека, ось) с делами. Форма доказана —
    значит пустота честная.

    Окно без такого соседа остаётся подозрением и в закрытые не идёт.
    """
    proof = harvest_task.alias("proof")
    return and_(
        harvest_task.c.status == "empty",
        select(1)
        .where(
            proof.c.court_domain == harvest_task.c.court_domain,
            proof.c.cartoteka_id == harvest_task.c.cartoteka_id,
            proof.c.axis == harvest_task.c.axis,
            proof.c.status == "done",
            proof.c.cases_found > 0,
        )
        .exists(),
    )


def _closed():
    """Окно, к которому возвращаться не надо."""
    return or_(harvest_task.c.status == "done", _confirmed_empty())


@dataclass(frozen=True, slots=True)
class Progress:
    windows_done: int
    windows_total: int
    #: Окна, где дел нет и это подтверждено соседним окном той же формы.
    windows_empty_confirmed: int
    #: Пустые окна без такого подтверждения — их надо разбирать руками.
    windows_empty_unconfirmed: int
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
        if not self.rate_per_hour or self.windows_done >= self.windows_total:
            return None
        rows_left = max(0, self.rows_expected - self.rows_collected)
        return (rows_left * AXES_FACTOR / PAGE_SIZE) / self.rate_per_hour


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
            select(func.count()).select_from(harvest_task).where(_closed())
        ).scalar_one()
        empty_confirmed = connection.execute(
            select(func.count()).select_from(harvest_task).where(_confirmed_empty())
        ).scalar_one()
        empty_total = connection.execute(
            select(func.count()).where(harvest_task.c.status == "empty")
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
        windows_empty_confirmed=empty_confirmed,
        windows_empty_unconfirmed=empty_total - empty_confirmed,
        rows_collected=rows_collected,
        # Ожидаем ровно замеренный корпус: две оси дают одни и те же дела,
        # а не вдвое больше. Пока здесь стояло удвоение, полный индекс
        # показывался как 52 % собранного — недосбор, которого нет.
        rows_expected=corpus,
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
                .where(_closed())
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
    if progress.windows_empty_confirmed:
        lines.append(
            f"          из них пусто, контроль есть: {_thousands(progress.windows_empty_confirmed)}"
        )
    if progress.windows_empty_unconfirmed:
        lines.append(
            f"          пусто БЕЗ контроля: "
            f"{_thousands(progress.windows_empty_unconfirmed)} — это подозрение, а не ноль"
        )
    lines.append(
        f"Собрано   {_thousands(progress.rows_collected)} дел"
        f" из ≈ {_thousands(progress.rows_expected)} замеренных"
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
