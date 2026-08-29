"""CLI каркаса: показать справочники, собрать URL, наполнить базу.

Живых запросов к судам здесь нет — на этом этапе проекта их нет нигде.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .directories import cartoteka, cartoteki, court, courts
from .urls import DateAxis, listing_url, page_count

#: Команды, которые ходят на суды. Каждая берёт замок на машину: общий
#: дроссель считает время в переменной процесса, поэтому два обхода разом
#: дали бы двойной темп — и 429, как 20.08.2026. Диагностика (`harvest
#: --pilot`) в списке намеренно: блокировку 19.08 принесли именно разовые
#: проверки мимо дросселя.
NETWORK_COMMANDS = frozenset({"harvest", "measure", "run", "catchup", "cards"})

#: Кто не отказывается, а ждёт очереди. Добег обязан отработать: свод
#: карточек держит замок почти непрерывно шесть суток, и «занято — выходим»
#: означало бы, что свежая практика не попадёт в индекс ни разу. Остальным
#: ждать нечего: свод поднимет таймер через полчаса, а разовую команду
#: запустил человек и он же решит, когда повторить.
WAIT_FOR_LOCK = frozenset({"catchup"})


def _parse_date(value: str):
    return datetime.strptime(value, "%d.%m.%Y").date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harvester")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("courts", help="показать справочник кассационных судов")
    sub.add_parser("cartoteki", help="показать справочник картотек")
    sub.add_parser("load-directories", help="записать справочники в базу")

    url = sub.add_parser("url", help="собрать URL перечня и напечатать его")
    url.add_argument("--court", required=True, help="домен суда, напр. 2kas.sudrf.ru")
    url.add_argument("--cartoteka", required=True, help="ключ картотеки: g3 | u3 | p3 | adm3")
    url.add_argument(
        "--axis",
        default="publication",
        choices=[axis.value for axis in DateAxis],
        help="ось дат: entry | result | publication",
    )
    url.add_argument("--from", dest="date_from", required=True, help="дд.мм.гггг")
    url.add_argument("--to", dest="date_to", required=True, help="дд.мм.гггг")
    url.add_argument("--page", type=int, default=1)

    run = sub.add_parser("harvest", help="обойти окно и записать дела в базу")
    run.add_argument("--court", required=True)
    run.add_argument("--cartoteka", required=True)
    run.add_argument("--axis", default="publication", choices=[axis.value for axis in DateAxis])
    run.add_argument("--from", dest="date_from", required=True, help="дд.мм.гггг")
    run.add_argument("--to", dest="date_to", required=True, help="дд.мм.гггг")
    run.add_argument(
        "--pilot",
        type=int,
        metavar="N",
        help="наблюдаемый пилот: не больше N страниц и без требования ночного окна. "
        "Массовый обход идёт без этого флага и только ночью.",
    )

    measure = sub.add_parser("measure", help="замерить объём картотек (счётчик без дат)")
    measure.add_argument("--court", action="append", help="ограничить суды, можно повторять")
    measure.add_argument("--cartoteka", action="append", help="ограничить картотеки")
    measure.add_argument("--again", action="store_true", help="перемерить уже измеренные пары")
    measure.add_argument("--pilot", action="store_true", help="без требования ночного окна")

    plan_cmd = sub.add_parser("plan", help="нарезать глубину на окна и наполнить очередь")
    plan_cmd.add_argument("--from", dest="start", help="дд.мм.гггг, по умолчанию 01.10.2019")
    plan_cmd.add_argument("--to", dest="end", help="дд.мм.гггг, по умолчанию сегодня")
    plan_cmd.add_argument("--court", action="append")
    plan_cmd.add_argument("--cartoteka", action="append")
    plan_cmd.add_argument(
        "--axis",
        default="publication",
        choices=[axis.value for axis in DateAxis],
        help="ось дат: publication — дела с опубликованным актом; "
        "entry — все дела, включая нерассмотренные",
    )

    sub.add_parser("queue", help="показать состояние очереди")
    sub.add_parser("status", help="состояние сбора: темп, остаток, отказы")

    find = sub.add_parser("search", help="искать по собранному индексу")
    find.add_argument("--court", action="append", help="домен суда, можно повторять")
    find.add_argument("--cartoteka", action="append", help="ключ картотеки")
    find.add_argument("--judge", action="append", help="судья, точное имя из выдачи")
    find.add_argument("--result", action="append", help="результат, точная формулировка")
    find.add_argument("--lower-court", action="append", help="суд первой инстанции")
    find.add_argument("--number", help="часть номера дела")
    find.add_argument("--from", dest="decided_from", help="дата решения от, дд.мм.гггг")
    find.add_argument("--to", dest="decided_to", help="дата решения до, дд.мм.гггг")
    find.add_argument("--with-act", action="store_true", help="только с опубликованным актом")
    find.add_argument("--facets", action="store_true", help="показать счётчики по фасетам")
    find.add_argument("--limit", type=int, default=25)
    find.add_argument("--offset", type=int, default=0)

    catch = sub.add_parser("catchup", help="добрать опубликованное за последние дни")
    catch.add_argument("--days", type=int, default=3, help="сколько дней назад, по умолчанию 3")
    catch.add_argument("--court", action="append")
    catch.add_argument("--pilot", action="store_true", help="без требования ночного окна")

    embed_cmd = sub.add_parser("embed", help="нарезать акты и посчитать эмбеддинги")
    embed_cmd.add_argument("--limit", type=int, help="сколько кусков посчитать за раз")

    dump_cmd = sub.add_parser("dump", help="снять дамп базы и положить в хранилище")
    dump_cmd.add_argument("--keep", type=int, default=7, help="сколько дампов держать")
    dump_cmd.add_argument("--force", action="store_true", help="снять, даже если за сегодня есть")

    archive = sub.add_parser("archive", help="выгрузить сырьё и веса в объектное хранилище")
    archive.add_argument("--dry-run", action="store_true", help="показать, но не заливать")
    archive.add_argument("--model", action="store_true", help="залить и веса решателя капчи")
    archive.add_argument(
        "--restore",
        action="store_true",
        help="наоборот: забрать сырьё из архива на диск (проверка, что копия разворачивается)",
    )
    archive.add_argument("--limit", type=int, help="при --restore: взять не больше N файлов")

    runner = sub.add_parser("run", help="прогнать очередь: собрать перечни по окнам")
    runner.add_argument("--court", action="append", help="ограничить суды, можно повторять")
    runner.add_argument("--limit", type=int, metavar="N", help="не больше N окон на суд")
    runner.add_argument(
        "--pilot", action="store_true", help="наблюдаемый прогон: без требования ночного окна"
    )

    acts = sub.add_parser("cards", help="обойти карточки дел: тексты актов, участники, движение")
    acts.add_argument("--court", help="домен суда, напр. 5kas.sudrf.ru")
    acts.add_argument("--all", action="store_true", help="все суды кругами, вместо одного")
    acts.add_argument("--cartoteka", help="ограничить картотекой: g3 | u3 | p3 | adm3")
    acts.add_argument("--limit", type=int, help="взять не больше N карточек за прогон")
    acts.add_argument(
        "--with-act",
        action="store_true",
        help="только дела со ссылкой на акт: 1,6 млн вместо 2,7 — вдвое короче срок",
    )
    acts.add_argument(
        "--since",
        help="только дела с датой решения не раньше этой, дд.мм.гггг",
    )
    acts.add_argument(
        "--pilot",
        action="store_true",
        help="наблюдаемый прогон: без требования ночного окна",
    )

    args = parser.parse_args(argv)

    if args.command in NETWORK_COMMANDS:
        from .http import AlreadyHarvesting, claim_harvest_lock

        try:
            claim_harvest_lock()
        except AlreadyHarvesting as exc:
            if args.command not in WAIT_FOR_LOCK:
                print(exc)
                return 1
            # Печатью, а не журналом: логи настраиваются ниже, каждой
            # командой отдельно, и до них сообщение бы не дожило. А знать
            # надо сразу — снаружи «ждёт очереди» и «повис» неотличимы.
            print(f"{exc}; жду очереди", flush=True)
            claim_harvest_lock(wait=True)

    if args.command == "courts":
        for item in courts():
            mark = "капча" if item.has_captcha else "—"
            number = item.number if item.number is not None else "—"
            print(f"{number:>2}  {item.domain:<16} {mark:<6} {item.title}")
        return 0

    if args.command == "cartoteki":
        for item in cartoteki():
            print(
                f"{item.id:<5} delo_id={item.delo_id:<8} new={item.new:<8} "
                f"{item.delo_table:<11} {item.doc_prefix:<16} {item.title}"
            )
        return 0

    if args.command == "load-directories":
        from .db.load_directories import load

        loaded_courts, loaded_cartoteki = load()
        print(f"судов: {loaded_courts}, картотек: {loaded_cartoteki}")
        return 0

    if args.command == "harvest":
        import logging

        from .harvest import harvest_listing

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        result = harvest_listing(
            court(args.court),
            cartoteka(args.cartoteka),
            DateAxis(args.axis),
            _parse_date(args.date_from),
            _parse_date(args.date_to),
            bulk=args.pilot is None,
            max_pages=args.pilot,
        )
        print(
            f"обход {result.run_id}: {result.status}, счётчик {result.expected}, "
            f"дел {result.cases}, ссылок на акты {result.acts}, страниц {result.pages}"
        )
        if result.note:
            print(result.note)
        return 0 if result.status in ("complete", "pilot") else 1

    if args.command == "measure":
        import logging

        from .volume import measure_all

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        results = measure_all(
            only_courts=args.court,
            only_cartoteki=args.cartoteka,
            bulk=not args.pilot,
            skip_measured=not args.again,
        )
        measured = [r for r in results if r.status == "measured"]
        total = sum(r.total_cases or 0 for r in measured)
        print(f"замерено пар: {len(measured)} из {len(results)}, дел суммарно: {total}")
        for r in results:
            if r.status != "measured":
                print(f"  {r.court_domain}/{r.cartoteka_id}: {r.status} — {r.note or ''}")
        return 0

    if args.command == "plan":
        from .plan import CORPUS_START, fill_queue

        added, existed = fill_queue(
            axis=DateAxis(args.axis),
            start=_parse_date(args.start) if args.start else CORPUS_START,
            end=_parse_date(args.end) if args.end else None,
            only_courts=args.court,
            only_cartoteki=args.cartoteka,
        )
        print(f"заданий добавлено: {added}, уже было: {existed}")
        return 0

    if args.command == "embed":
        import logging

        from sqlalchemy import create_engine

        from .config import settings
        from .embeddings import fill_embeddings, split_pending

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        engine = create_engine(settings.database_url)
        print(f"нарезано кусков: {split_pending(engine)}")
        print(f"посчитано эмбеддингов: {fill_embeddings(engine, limit=args.limit)}")
        return 0

    if args.command == "dump":
        import logging

        from .dump import make_dump
        from .s3 import S3Store

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        key = make_dump(S3Store(), keep=args.keep, force=args.force)
        print(f"дамп выгружен: {key}" if key else "дамп за сегодня уже есть")
        return 0

    if args.command == "search":
        from datetime import datetime

        from sqlalchemy import create_engine

        from .config import settings
        from .search import Query, run

        def as_date(value):
            return datetime.strptime(value, "%d.%m.%Y").date() if value else None

        engine = create_engine(settings.database_url)
        found = run(
            engine,
            Query(
                courts=tuple(args.court or ()),
                cartoteki=tuple(args.cartoteka or ()),
                judges=tuple(args.judge or ()),
                results=tuple(args.result or ()),
                lower_courts=tuple(args.lower_court or ()),
                number=args.number,
                decided_from=as_date(args.decided_from),
                decided_to=as_date(args.decided_to),
                with_act=True if args.with_act else None,
                limit=args.limit,
                offset=args.offset,
            ),
            with_facets=args.facets,
        )

        print(f"найдено {found.total} дел из {len(found.rows)} показанных")
        print("это поиск ПО СОБРАННОМУ: индекс ещё наполняется\n")
        for row in found.rows:
            act = {True: "акт есть", False: "акта нет", None: "акт не проверен"}[
                row["act_published"]
            ]
            print(
                f"  {row['case_number']:28} {row['court_domain']:16}"
                f" {str(row['decision_date'] or '—'):12} {act}"
            )
            if row["judge"] or row["result"]:
                print(f"       {row['judge'] or '—'} · {(row['result'] or '—')[:70]}")
        for name, values in found.facets.items():
            print(f"\n{name}:")
            for value, count in values:
                print(f"  {count:>8}  {value[:70]}")
        return 0

    if args.command == "status":
        from sqlalchemy import create_engine, func, select

        from . import status as status_module
        from .config import settings

        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            now = connection.execute(select(func.now())).scalar_one()
        print(
            status_module.render(
                status_module.collect(engine, now=now),
                status_module.by_court(engine),
                now=now,
            )
        )
        return 0

    if args.command == "queue":
        from sqlalchemy import create_engine, func, select

        from .config import settings
        from .db.schema import harvest_task

        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            rows = connection.execute(
                select(
                    harvest_task.c.status,
                    func.count().label("заданий"),
                    func.min(harvest_task.c.window_from).label("от"),
                    func.max(harvest_task.c.window_to).label("до"),
                ).group_by(harvest_task.c.status)
            ).all()
        engine.dispose()
        if not rows:
            print("очередь пуста — сперва `plan`")
            return 0
        for row in rows:
            print(f"{row.status:<10} {row[1]:>6}  {row[2]} … {row[3]}")
        return 0

    if args.command == "catchup":
        import logging

        from .catchup import catchup

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        result = catchup(days=args.days, only_courts=args.court, bulk=not args.pilot)
        print(
            f"окон {result.windows}, дел {result.cases}, ссылок на акты {result.acts}, "
            f"пропущено судов {result.skipped}, неудач {result.failed}"
        )
        for problem in result.problems:
            print(f"  {problem}")
        return 0

    if args.command == "archive":
        import logging

        from .archive import model_upload, push, raw_uploads
        from .config import settings

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        if not settings.s3_bucket:
            print("не задан бакет: HARVESTER_S3_BUCKET (и ключи доступа)")
            return 1

        from .s3 import S3Store

        store = S3Store()

        if args.restore:
            from .archive import pull

            restored = pull(store, settings.raw_root, limit=args.limit, dry_run=args.dry_run)
            print(f"скачано {restored.downloaded}, уже было {restored.skipped}")
            return 0

        uploads = list(raw_uploads(settings.raw_root))
        if args.model:
            weights = model_upload(Path("data/captcha-model.json"))
            if weights is not None:
                uploads.append(weights)

        result = push(store, uploads, dry_run=args.dry_run)
        print(
            f"выгружено {result.uploaded}, уже было {result.skipped}, "
            f"объём {result.bytes_sent / 2**20:.1f} МБ"
        )
        return 0

    if args.command == "run":
        import logging

        from .run import run_queue

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        totals = run_queue(
            only_courts=args.court,
            bulk=not args.pilot,
            limit_per_court=args.limit,
        )
        print(
            f"окон пройдено: {totals.windows}, дел {totals.cases}, ссылок на акты "
            f"{totals.acts}, неудач {totals.failed}, придержаний {totals.throttled}"
        )
        return 0

    if args.command == "cards":
        import logging

        from .cards import collect_cards, sweep_all

        if not args.court and not args.all:
            parser.error("нужен --court или --all")

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        since = _parse_date(args.since) if args.since else None
        if args.all:
            results = sweep_all(cartoteka_id=args.cartoteka, with_act=args.with_act, since=since)
        else:
            results = [
                collect_cards(
                    args.court,
                    limit=args.limit,
                    bulk=not args.pilot,
                    cartoteka_id=args.cartoteka,
                    with_act=args.with_act,
                    since=since,
                )
            ]
        for result in results:
            print(
                f"{result.court_domain}: карточек {result.cards} из {result.attempted}, "
                f"текстов {result.texts}, участников {result.participants}, "
                f"без текста {result.without_text}, ошибок {result.failed}, "
                f"осталось {result.remaining}"
            )
        if any(result.throttled for result in results):
            print("суд попросил перестать — продолжать после паузы")
            return 1
        return 0

    if args.command == "url":
        target = listing_url(
            court(args.court),
            cartoteka(args.cartoteka),
            DateAxis(args.axis),
            _parse_date(args.date_from),
            _parse_date(args.date_to),
            page=args.page,
        )
        print(target)
        print(f"# страниц при N делах: ceil(N / 25), напр. 530 → {page_count(530)}")
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
