"""Прохождение капчи: перебор прочтений и отступление после отказа."""

from __future__ import annotations

import numpy as np
import pytest

from harvester.captcha.model import ARCH
from harvester.captcha.solve import candidates


class _FakeModel:
    """Модель с заданными вероятностями — чтобы проверять порядок кандидатов,
    а не саму сеть."""


def _probs(rows: list[list[float]]) -> np.ndarray:
    return np.asarray(rows, dtype=np.float32)


@pytest.fixture
def five_heads(monkeypatch):
    """Пять голов: четыре уверенные, пятая сомневается между 7 и 1."""
    probs = np.zeros((ARCH["HEADS"], ARCH["NC"]), dtype=np.float32)
    for head, digit in enumerate([1, 2, 3, 4]):
        probs[head, digit] = 0.99
        probs[head, (digit + 1) % 10] = 0.01
    probs[4, 7] = 0.55
    probs[4, 1] = 0.45

    monkeypatch.setattr("harvester.captcha.solve.probabilities", lambda *a, **k: probs)
    return probs


def test_first_candidate_is_the_argmax(five_heads) -> None:
    result = candidates(_FakeModel(), np.zeros((20, 64), dtype=np.float32))
    assert result[0][0] == "12347"


def test_least_confident_digit_is_swapped_first(five_heads) -> None:
    """Ошибается модель почти всегда в одной цифре, и вероятнее всего —
    в той, где она сомневалась сильнее. Значит её и проверяем второй."""
    result = candidates(_FakeModel(), np.zeros((20, 64), dtype=np.float32))
    assert result[1][0] == "12341"


def test_candidates_are_ordered_and_limited(five_heads) -> None:
    result = candidates(_FakeModel(), np.zeros((20, 64), dtype=np.float32), limit=3)
    assert len(result) == 3
    likelihoods = [likelihood for _, likelihood in result]
    assert likelihoods == sorted(likelihoods, reverse=True)
    assert len({text for text, _ in result}) == 3


def test_cooldown_blocks_further_requests() -> None:
    """После отказа суд не трогаем: продолжать стучаться — это и есть
    накопление блокировки."""
    from harvester.http import CourtClient, CourtOnCooldown

    client = CourtClient(bulk=False)
    try:
        client.back_off("2kas.sudrf.ru", 600, "проверка")
        assert client.cooldown_left("2kas.sudrf.ru") > 500
        with pytest.raises(CourtOnCooldown, match="паузы"):
            client.get("https://2kas.sudrf.ru/modules.php")
        # Другой суд не при чём.
        assert client.cooldown_left("5kas.sudrf.ru") == 0
    finally:
        from harvester.http import _COOLDOWNS

        _COOLDOWNS.clear()
        client.close()


def test_cooldown_is_shared_between_clients() -> None:
    """Клиент создаётся на каждую задачу, а суд один: следующая задача
    не должна начинать с того, чем предыдущая заслужила отказ."""
    from harvester.http import _COOLDOWNS, CourtClient

    first, second = CourtClient(bulk=False), CourtClient(bulk=False)
    try:
        first.back_off("3kas.sudrf.ru", 600, "проверка")
        assert second.cooldown_left("3kas.sudrf.ru") > 500
    finally:
        _COOLDOWNS.clear()
        first.close()
        second.close()


def test_throttle_marker_is_found_in_cp1251_bytes() -> None:
    """Маркер ищется до перекодировки — дешевле и раньше."""
    from harvester.http import _looks_throttled

    assert _looks_throttled("Информация временно недоступна".encode("cp1251"))
    assert not _looks_throttled("Всего по запросу найдено — 530".encode("cp1251"))
