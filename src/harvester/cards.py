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
from .http import CourtOnCooldown
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


#: Сколько карточек берётся у одного суда за круг. Мелкими кругами, а не
#: судом целиком: иначе первый же суд занял бы недели, и до остальных
#: очередь дошла бы через месяц.
ROUND_CHUNK = 200


def sweep_all(
    *,
    settings: Settings | None = None,
    chunk: int = ROUND_CHUNK,
    cartoteka_id: str | None = None,
    with_act: bool = False,
    since: date | None = None,
) -> list[CardSweepResult]:
    """Обойти карточки всех судов кругами, пока они не кончатся.

    Потоков нет намеренно: темп задаёт общий дроссель платформы (1,5 с
    между любыми двумя запросами), а не суд, поэтому десять потоков дали бы
    тот же час и десять поводов получить 429.

    Суд, попросивший отступить, из круга НЕ выбывает — он пропускается
    и берётся снова на следующем. Выбывать насовсем ему нельзя: свод идёт
    сутками, а пауза длится полчаса, и один отказ стоил бы суду всех
    оставшихся дней.

    Круг, в котором ни один суд не отдал ни карточки, заканчивает прогон:
    значит либо всё собрано, либо все отдыхают. Поднимет заново `launchd`.
    """
    from .directories import courts

    live = [item.domain for item in courts()]
    totals: dict[str, CardSweepResult] = {}

    while live:
        worked = False
        for domain in list(live):
            result = collect_cards(
                domain,
                settings=settings,
                limit=chunk,
                cartoteka_id=cartoteka_id,
                with_act=with_act,
                since=since,
            )
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
            if result.cards:
                worked = True
            if result.remaining == 0:
                live.remove(domain)
        if not worked:
            break

    return [totals[domain] for domain in sorted(totals)]
