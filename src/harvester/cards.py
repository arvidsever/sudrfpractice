"""Сбор карточек дел: тексты актов и всё, чего нет в перечне.

Карточка стоит столько же, сколько страница акта, — один запрос, — но
отдаёт сверх текста участников с реквизитами, суд первой инстанции
и движение по событиям. При нескольких актах на дело она отдаёт все
вкладки разом, тогда как `name_op=doc` требует запроса на каждую.
И капчей она не защищена ни на одном суде: на капча-судах капча нужна
только для перечня.

Полнота свода измеряется базой, а не памятью о прогоне: берутся дела
с непроставленным `card_fetched_at`. Прерванный сбор продолжается сам,
повторный запуск ничего не перекачивает.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert

from .client import open_client
from .config import Settings
from .config import settings as default_settings
from .db.schema import act_text, case
from .db.store import act_for_text, save_card, save_raw_page
from .directories import cartoteka as find_cartoteka
from .directories import court as find_court
from .guards import Verdict, classify
from .http import CourtOnCooldown, cooldown_left, wait_out_cooldown
from .parse.card import parse_card
from .raw import RawStore
from .urls import card_url

log = logging.getLogger("harvester.cards")

#: Ниже этого числа знаков текст считается подозрительным: кассационные
#: акты — тысячи знаков. Короткий ответ значит, что мы получили не акт.
MIN_PLAUSIBLE_LENGTH = 200


@dataclass(slots=True)
class CardSweepResult:
    court_domain: str
    attempted: int
    cards: int
    texts: int
    participants: int
    without_text: int
    failed: int
    remaining: int
    throttled: bool = False


def pending_cards(
    connection,
    court_domain: str,
    limit: int | None = None,
    cartoteka_id: str | None = None,
    *,
    with_act: bool = False,
    since: date | None = None,
):
    """Дела этого суда, чью карточку ещё не открывали, от свежего к старому.

    Порядок и отбор — это цена этапа: весь индекс стоит около 57 суток,
    только дела со ссылкой на акт — 33, они же с 2025 года — 8. Поэтому
    `with_act` и `since` не украшение, а способ выбрать срок.

    Дела без даты решения (найденные по оси поступления и ещё не
    рассмотренные) уходят в конец: ссылка на акт есть у считанных единиц,
    а карточку всё равно придётся перечитывать после рассмотрения.
    """
    conditions = [
        case.c.court_domain == court_domain,
        case.c.card_fetched_at.is_(None),
        case.c.case_id.is_not(None),
        case.c.case_uid.is_not(None),
    ]
    if cartoteka_id is not None:
        conditions.append(case.c.cartoteka_id == cartoteka_id)
    if with_act:
        conditions.append(case.c.act_published.is_(True))
    if since is not None:
        conditions.append(case.c.decision_date >= since)

    query = (
        select(case.c.id, case.c.case_id, case.c.case_uid, case.c.cartoteka_id, case.c.case_number)
        .where(*conditions)
        .order_by(case.c.decision_date.desc().nullslast(), case.c.id.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    return connection.execute(query).all()


def collect_cards(
    court_domain: str,
    *,
    settings: Settings | None = None,
    limit: int | None = None,
    bulk: bool = True,
    cartoteka_id: str | None = None,
    with_act: bool = False,
    since: date | None = None,
) -> CardSweepResult:
    settings = settings or default_settings
    raw_store = RawStore(settings.raw_root)
    engine = create_engine(settings.database_url)
    court = find_court(court_domain)

    with engine.connect() as connection:
        targets = pending_cards(
            connection, court_domain, limit, cartoteka_id, with_act=with_act, since=since
        )

    cards = texts = participants = without_text = failed = 0
    throttled = False

    with open_client(court, settings=settings, bulk=bulk) as client:
        for row in targets:
            cartoteka = find_cartoteka(row.cartoteka_id)
            url = card_url(
                court, row.case_id, row.case_uid, cartoteka.listing_delo_id, cartoteka.new
            )
            try:
                response = client.get(url)
            except CourtOnCooldown as exc:
                # Суд отдыхает — остальные его карточки ждать не будут, иначе
                # остаток свода превратится в тысячи мгновенных «неудач»,
                # не сделавших ни одного запроса.
                throttled = True
                log.info("%s", exc)
                break
            except Exception as exc:  # noqa: BLE001 — одна карточка не роняет свод
                failed += 1
                log.warning("%s: %s", row.case_number, exc)
                continue

            html = response.text
            if classify(html).verdict is Verdict.THROTTLED:
                throttled = True
                log.warning("суд ответил «Информация временно недоступна», свод остановлен")
                break

            record = raw_store.save(
                response.content,
                url=url,
                court_domain=court_domain,
                http_status=response.status_code,
                content_kind="card",
            )
            card = parse_card(html)

            with engine.begin() as connection:
                raw_id = save_raw_page(connection, record)
                save_card(connection, row.id, card)
                participants += len(card.participants)

                stored_here = 0
                for text_number, text in sorted(card.act_texts.items()):
                    if len(text) < MIN_PLAUSIBLE_LENGTH:
                        continue
                    act_id = act_for_text(connection, row.id, text_number)
                    statement = insert(act_text).values(
                        act_id=act_id, raw_page_id=raw_id, plain_text=text
                    )
                    connection.execute(
                        statement.on_conflict_do_update(
                            index_elements=["act_id"],
                            set_={
                                "plain_text": statement.excluded.plain_text,
                                "raw_page_id": statement.excluded.raw_page_id,
                            },
                        )
                    )
                    stored_here += 1

            texts += stored_here
            cards += 1
            if stored_here == 0:
                # 262-ФЗ: публикуется не всё. Карточку мы открыли, значит
                # знание есть — просто текста в ней нет.
                without_text += 1

    with engine.connect() as connection:
        remaining = len(
            pending_cards(
                connection, court_domain, cartoteka_id=cartoteka_id, with_act=with_act, since=since
            )
        )
    engine.dispose()

    return CardSweepResult(
        court_domain=court_domain,
        attempted=len(targets),
        cards=cards,
        texts=texts,
        participants=participants,
        without_text=without_text,
        failed=failed,
        remaining=remaining,
        throttled=throttled,
    )


#: Сколько карточек поток берёт у своего суда за один заход. Кусками, а не
#: судом целиком: между заходами свод переспрашивает базу, и прерванный
#: прогон не теряет счёт остатка.
ROUND_CHUNK = 200

#: Сколько пустых заходов подряд суд получает, прежде чем поток уйдёт.
#: Один пустой заход — это не «дел нет», а чаще «суд сейчас не в духе»:
#: 31.08.2026 многочасовой шторм таймаутов выгнал четыре суда из семи,
#: и вернуть их мог только новый процесс.
EMPTY_ROUNDS_BEFORE_LEAVING = 5

#: Пауза между пустыми заходами. Меньше получасового отступления: суд
#: нас не придерживал, ему просто нехорошо.
EMPTY_ROUND_PAUSE_SECONDS = 120.0

#: Сколько часов работает один прогон. Не от зависаний: свод обязан
#: иногда отпускать замок, иначе суточный добег не дождётся очереди.
#: `RuntimeMaxSec` в systemd для этого не годится — к `Type=oneshot`
#: он не применяется и молча ничего не делает.
MAX_RUN_HOURS = 6.0


def sweep_all(
    *,
    settings: Settings | None = None,
    chunk: int = ROUND_CHUNK,
    cartoteka_id: str | None = None,
    with_act: bool = False,
    since: date | None = None,
    max_hours: float = MAX_RUN_HOURS,
) -> list[CardSweepResult]:
    """Обойти карточки всех судов — поток на суд, пока они не кончатся.

    **Потоки здесь не про параллельность, а про то, какой дроссель
    окажется главным.** Первая версия шла судами по очереди, и замер это
    сразу показал: 3,0 с между запросами, 1 200 в час. Упирался дроссель
    НА ХОСТ (3 с), потому что в каждый момент открыт был ровно один суд.
    Общий дроссель (1,5 с) при этом простаивал: ему нечего сдерживать,
    пока запросы и так идут вдвое реже.

    С потоком на суд пауза одного суда закрывается работой соседей,
    и главным становится общий дроссель — 2 400 запросов в час, то есть
    вдвое. Быстрее не будет: это потолок платформы, а не наш.

    Суд, попросивший отступить, поток не бросает, а досыпает паузу —
    ровно как в прогоне очереди. Свод идёт сутками, пауза длится полчаса,
    и уход означал бы, что суд, однажды придержавший нас, больше
    не собирается никогда.

    **Пустой заход тоже не повод уходить насовсем.** 31.08.2026 суды
    несколько часов отвечали таймаутами; заход в 200 карточек не дал
    ни одной, и потоки 1, 2, 4 и 7 КСОЮ вышли — навсегда, потому что
    поднять их мог только новый процесс. К утру из семи судов работали
    три, темп упал с 2 320 запросов в час до 105. Теперь уход только
    после `EMPTY_ROUNDS_BEFORE_LEAVING` пустых заходов подряд.

    **И сам прогон ограничен по времени.** Не от зависаний: свод обязан
    иногда отпускать замок, иначе суточный добег не дождётся очереди.
    Ограничение стоит здесь, а не в systemd, потому что `RuntimeMaxSec`
    к `Type=oneshot` не применяется вовсе — оно там молча ничего не делало.
    """
    from .directories import courts

    domains = [item.domain for item in courts()]
    totals: dict[str, CardSweepResult] = {}
    guard = threading.Lock()
    stop = threading.Event()
    deadline = time.monotonic() + max_hours * 3600

    def work(domain: str) -> None:
        empty_rounds = 0
        while not stop.is_set():
            if time.monotonic() > deadline:
                log.info("%s: время прогона вышло, отпускаем замок", domain)
                return
            if not wait_out_cooldown(domain, stop):
                return
            result = collect_cards(
                domain,
                settings=settings,
                limit=chunk,
                cartoteka_id=cartoteka_id,
                with_act=with_act,
                since=since,
            )
            with guard:
                previous = totals.get(domain)
                totals[domain] = (
                    result
                    if previous is None
                    else CardSweepResult(
                        court_domain=domain,
                        attempted=previous.attempted + result.attempted,
                        cards=previous.cards + result.cards,
                        texts=previous.texts + result.texts,
                        participants=previous.participants + result.participants,
                        without_text=previous.without_text + result.without_text,
                        failed=previous.failed + result.failed,
                        remaining=result.remaining,
                        throttled=result.throttled,
                    )
                )
            log.info(
                "%s: карточек %d, текстов %d, осталось %d",
                domain,
                result.cards,
                result.texts,
                result.remaining,
            )
            if result.remaining == 0:
                return
            if result.cards:
                empty_rounds = 0
                continue
            if cooldown_left(domain) > 0:
                continue  # суд придержал — досыпаем паузу, это не пустота
            # Заход без карточек и без паузы: суд молчит, отвечает
            # таймаутами или отдаёт не то. Крутиться вхолостую нельзя,
            # но и уходить с первого раза — тоже: так свод и выродился
            # в три суда из семи. Даём суду отдышаться и пробуем снова.
            empty_rounds += 1
            if empty_rounds >= EMPTY_ROUNDS_BEFORE_LEAVING:
                log.warning("%s: %d пустых захода подряд, поток уходит", domain, empty_rounds)
                return
            stop.wait(EMPTY_ROUND_PAUSE_SECONDS)

    threads = [
        threading.Thread(target=work, args=(domain,), name=f"карточки-{domain}", daemon=True)
        for domain in domains
    ]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:  # pragma: no cover — сценарий человека
        log.warning("прерывание: доводим текущие куски и выходим")
        stop.set()
        for thread in threads:
            thread.join(timeout=120)

    return [totals[domain] for domain in sorted(totals)]
