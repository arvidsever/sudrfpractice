"""Письмо о падении задания: когда писать и когда молчать.

24–26.09.2026 выгрузка 42 часа падала каждые шесть часов, и узнали
об этом случайно. Но и будить из-за каждой неудачи нельзя: 22.09 минутный
сбой DNS провайдера исправил следующий же заход.
"""

from __future__ import annotations

import pytest

from harvester.alert import handle

UNIT = "sudrf-archive.service"


def _run(results, state, send):
    return [handle(UNIT, result, state, send) for result in results]


def test_one_failure_is_not_worth_a_letter(tmp_path) -> None:
    sent: list[str] = []
    _run(["exit-code", "success"], tmp_path, lambda s, b: sent.append(s))
    assert sent == []


def test_second_failure_in_a_row_writes_once_and_recovery_closes_it(tmp_path) -> None:
    """Одно письмо на всю полосу неудач, а не каждые шесть часов,
    и одно — когда полоса кончилась, чтобы тревога не оставалась открытой."""
    sent: list[str] = []
    _run(
        ["exit-code", "exit-code", "exit-code", "exit-code", "success"],
        tmp_path,
        lambda s, b: sent.append(s),
    )
    assert sent == [f"{UNIT}: упало 2 раз подряд", f"{UNIT}: снова работает"]


def test_letter_that_did_not_go_is_retried_not_forgotten(tmp_path) -> None:
    """Метка «уже написали» ставится после отправки. Не ушло письмо —
    пробуем на следующей неудаче; иначе первая же ошибка почты
    заглушила бы тревоги до конца полосы."""

    def broken(subject, body):
        raise OSError("smtp недоступен")

    handle(UNIT, "exit-code", tmp_path, broken)
    with pytest.raises(OSError):
        handle(UNIT, "exit-code", tmp_path, broken)

    sent: list[str] = []
    handle(UNIT, "exit-code", tmp_path, lambda s, b: sent.append(s))
    assert sent == [f"{UNIT}: упало 3 раз подряд"]


def test_success_resets_the_count(tmp_path) -> None:
    sent: list[str] = []
    _run(["exit-code", "success", "exit-code"], tmp_path, lambda s, b: sent.append(s))
    assert sent == []
