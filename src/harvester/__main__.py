"""CLI каркаса: показать справочники, собрать URL, наполнить базу.

Живых запросов к судам здесь нет — на этом этапе проекта их нет нигде.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from .directories import cartoteka, cartoteki, court, courts
from .urls import DateAxis, listing_url, page_count


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

    acts = sub.add_parser("acts", help="скачать тексты актов, у которых их ещё нет")
    acts.add_argument("--court", required=True, help="домен суда, напр. 5kas.sudrf.ru")
    acts.add_argument("--limit", type=int, help="взять не больше N актов за прогон")
    acts.add_argument(
        "--pilot",
        action="store_true",
        help="наблюдаемый прогон: без требования ночного окна",
    )

    args = parser.parse_args(argv)

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

    if args.command == "acts":
        import logging

        from .acts import collect_act_texts

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        result = collect_act_texts(args.court, limit=args.limit, bulk=not args.pilot)
        print(
            f"{result.court_domain}: взято {result.attempted}, записано {result.stored}, "
            f"пустых {result.empty}, ошибок {result.failed}, осталось {result.remaining}"
        )
        if result.throttled:
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
