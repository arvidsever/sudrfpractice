"""Круговой обход судов: чем он обязан кончиться и чем — нет.

Свод карточек идёт сутками, а пауза суда длится полчаса. Значит суд,
попросивший отступить, обязан вернуться следующим кругом: выбыви он
насовсем, один отказ стоил бы ему всех оставшихся дней сбора.
"""

from __future__ import annotations

from harvester import cards


def _result(domain: str, *, collected: int, remaining: int, throttled: bool = False):
    return cards.CardSweepResult(
        court_domain=domain,
        attempted=collected,
        cards=collected,
        texts=collected,
        participants=0,
        without_text=0,
        failed=0,
        remaining=remaining,
        throttled=throttled,
    )


def test_court_on_pause_comes_back_next_round(monkeypatch) -> None:
    """Пока хоть кто-то отдаёт карточки, придержанный суд берут снова.

    Это и есть разница между «пропустить» и «выбыть»: свод идёт сутками,
    пауза длится полчаса, и выбывший суд потерял бы все оставшиеся дни.
    """
    calls: list[str] = []
    scripted = {
        # Первый просит отступить, на втором круге отдаёт дела.
        "1kas.sudrf.ru": [
            _result("1kas.sudrf.ru", collected=0, remaining=5, throttled=True),
            _result("1kas.sudrf.ru", collected=5, remaining=0),
        ],
        # Второй работает всё это время — значит круг не холостой.
        "2kas.sudrf.ru": [
            _result("2kas.sudrf.ru", collected=3, remaining=3),
            _result("2kas.sudrf.ru", collected=3, remaining=0),
        ],
    }

    def fake(domain, **_):
        calls.append(domain)
        return scripted[domain].pop(0)

    monkeypatch.setattr(cards, "collect_cards", fake)
    monkeypatch.setattr(
        "harvester.directories.courts",
        lambda: [type("C", (), {"domain": d})() for d in ("1kas.sudrf.ru", "2kas.sudrf.ru")],
    )

    results = cards.sweep_all()

    assert calls.count("1kas.sudrf.ru") == 2, "придержанный суд обязан вернуться"
    assert {r.court_domain: r.cards for r in results} == {"1kas.sudrf.ru": 5, "2kas.sudrf.ru": 6}


def test_round_without_work_ends_the_run(monkeypatch) -> None:
    """Все придержаны — крутиться вхолостую нельзя, иначе прогон превратится
    в бесконечный цикл на нулевой работе. Поднимет заново launchd."""
    calls: list[str] = []

    def fake(domain, **_):
        calls.append(domain)
        return _result(domain, collected=0, remaining=99, throttled=True)

    monkeypatch.setattr(cards, "collect_cards", fake)
    monkeypatch.setattr(
        "harvester.directories.courts",
        lambda: [type("C", (), {"domain": d})() for d in ("1kas.sudrf.ru", "2kas.sudrf.ru")],
    )

    cards.sweep_all()

    assert calls == ["1kas.sudrf.ru", "2kas.sudrf.ru"], "ровно один холостой круг, и выходим"
