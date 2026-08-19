"""Обход сплошного перечня дел за окно дат.

Порядок обхода прост: страница 1 даёт счётчик, счётчик даёт число страниц,
дальше перебор до исчерпания. Сложность не в обходе, а в том, чтобы
недосбор нельзя было принять за пустой суд.

Поэтому обход устроен так:

* запись в `harvest_run` заводится ДО первого запроса. Прерванный обход
  остаётся в журнале как незакрытый, а не исчезает;
* каждая страница проходит `guards.classify`. Всё, кроме выдачи, —
  остановка с явным статусом, а не «дальше дел нет»;
* число разобранных строк сверяется со счётчиком на каждой странице;
* итог сверяется со счётчиком целиком: `short` в журнале означает, что
  окно надо перечитать.

Сырьё сохраняется до разбора, поэтому доработка парсера не потребует
второго обхода судов.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import create_engine

from .client import open_client
from .config import Settings
from .config import settings as default_settings
from .db import store
from .directories import Cartoteka, Court
from .guards import Verdict, classify, listing_kind, suspect_wrong_delo_id
from .http import CourtOnCooldown, OutsideCollectionWindow
from .parse.listing import parse_listing
from .raw import RawStore
from .urls import DateAxis, listing_url, page_count

log = logging.getLogger("harvester.harvest")


@dataclass(slots=True)
class HarvestResult:
    run_id: int
    expected: int | None
    cases: int
    acts: int
    pages: int
    status: str
    note: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"


def harvest_listing(
    court: Court,
    cartoteka: Cartoteka,
    axis: DateAxis,
    date_from: date,
    date_to: date,
    *,
    settings: Settings | None = None,
    bulk: bool = True,
    max_pages: int | None = None,
) -> HarvestResult:
    """Обойти окно и записать дела в базу.

    `bulk=False` снимает требование ночного окна — только для наблюдаемого
    пилота на несколько страниц. Массовый обход идёт с `bulk=True`.
    """
    settings = settings or default_settings
    raw_store = RawStore(settings.raw_root)
    engine = create_engine(settings.database_url)

    expected: int | None = None
    hidden_links = False
    cases = acts = pages_done = 0
    status = "failed"
    note: str | None = None

    with engine.begin() as connection:
        run_id = store.open_run(
            connection,
            court_domain=court.domain,
            cartoteka_id=cartoteka.id,
            axis=axis.value,
            window_from=date_from,
            window_to=date_to,
        )

    try:
        with open_client(court, cartoteka, settings=settings, bulk=bulk) as client:
            page = 1
            total_pages: int | None = None

            while total_pages is None or page <= total_pages:
                url = listing_url(court, cartoteka, axis, date_from, date_to, page=page)
                response = client.get_passing_captcha(url)

                record = raw_store.save(
                    response.content,
                    url=url,
                    court_domain=court.domain,
                    http_status=response.status_code,
                    content_kind="listing",
                )

                html = response.text
                state = classify(html)
                if state.verdict is Verdict.THROTTLED:
                    status = "throttled"
                    note = (
                        f"страница {page}: суд ответил «Информация временно недоступна». "
                        "Обход остановлен; продолжать после паузы, а не сразу."
                    )
                    log.warning(note)
                    break

                if state.verdict is not Verdict.LISTING:
                    status = "empty" if state.verdict is Verdict.NO_DATA else "failed"
                    note = (
                        f"страница {page}: {state.verdict.value}. "
                        "Пустая выдача неотличима от кривого запроса — "
                        "нужен контрольный запрос с заведомо непустым окном."
                    )
                    log.warning(note)
                    break

                listing = parse_listing(
                    html,
                    court_domain=court.domain,
                    cartoteka_id=cartoteka.id,
                    page=page,
                    state=state,
                )

                if expected is None:
                    kind = listing_kind(html)
                    if axis is DateAxis.PUBLICATION and suspect_wrong_delo_id(
                        sum(1 for row in listing.rows if row.act_links), len(listing.rows)
                    ):
                        log.warning(
                            "%s/%s: под осью публикации нет НИ ОДНОЙ ссылки на текст акта, "
                            "а выдача озаглавлена «%s». Похоже на короткий delo_id: счётчик "
                            "сойдётся, тексты будут недостижимы. "
                            "См. docs/delo-id-and-act-links.md",
                            court.domain,
                            cartoteka.id,
                            kind or "—",
                        )
                        hidden_links = True

                    expected = listing.total
                    total_pages = page_count(expected)
                    if max_pages is not None:
                        total_pages = min(total_pages, max_pages)
                    log.info(
                        "%s/%s: %d дел, %d страниц",
                        court.domain,
                        cartoteka.id,
                        expected,
                        total_pages,
                    )

                with engine.begin() as connection:
                    store.save_raw_page(connection, record)
                    for row in listing.rows:
                        case_pk = store.save_case(
                            connection,
                            row,
                            court_domain=court.domain,
                            cartoteka_id=cartoteka.id,
                        )
                        acts += store.save_acts(connection, case_pk, row)
                        cases += 1

                pages_done = page
                log.info("страница %d/%s: %d дел", page, total_pages, len(listing.rows))
                page += 1

            else:
                # Цикл дошёл до конца без break — обход завершён.
                if max_pages is not None and expected and pages_done < page_count(expected):
                    status = "pilot"
                    note = f"пилот: {pages_done} страниц из {page_count(expected)}"
                elif expected is not None and cases == expected:
                    status = "complete"
                    if hidden_links:
                        note = (
                            "реквизиты собраны полностью, но ссылок на тексты нет ни у одного "
                            "дела — вероятно, перечень запрошен с коротким delo_id. "
                            "См. docs/delo-id-and-act-links.md"
                        )
                else:
                    status = "short"
                    note = f"счётчик обещал {expected}, разобрано {cases}"

    except OutsideCollectionWindow as exc:
        # Собирать было нельзя — это не недосбор. Записанное сюда `failed`
        # заставило бы сверку полноты (этап 5.2) искать причину там, где
        # её нет: окно вернулось в очередь целым и соберётся ночью.
        status = "deferred"
        note = f"{type(exc).__name__}: {exc}"
        log.info("окно отложено: %s", exc)
        raise
    except CourtOnCooldown as exc:
        # Суд попросил отступить. Тот же статус, что и у придержания,
        # пойманного внутри обхода, — причина одна.
        status = "throttled"
        note = f"{type(exc).__name__}: {exc}"
        log.info("окно отложено: %s", exc)
        raise
    except Exception as exc:  # noqa: BLE001 — статус обхода важнее типа ошибки
        status = "failed"
        note = f"{type(exc).__name__}: {exc}"
        log.exception("обход прерван")
        raise
    finally:
        with engine.begin() as connection:
            store.close_run(
                connection,
                run_id,
                expected_count=expected,
                fetched_rows=cases,
                pages_done=pages_done,
                status=status,
                note=note,
            )
        engine.dispose()

    return HarvestResult(
        run_id=run_id,
        expected=expected,
        cases=cases,
        acts=acts,
        pages=pages_done,
        status=status,
        note=note,
    )
