"""Порт распознавателя капчи: сверка с оригиналом на JS.

Порт обязан совпадать с оригиналом, а не «работать похоже». Препроцессинг
несёт большую часть точности, и расхождение в нём выглядело бы не как
ошибка, а как плохая модель — то есть его нечем было бы заметить.

Эталон снят скриптом `dump-vectors.mjs` с `server/captcha.js` и лежит
в `tests/fixtures/captcha_reference.json`: имя файла → вход сети (1280
чисел). Сеть тут не участвует, поэтому эталон не устаревает при
переобучении.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from harvester.captcha.preprocess import (
    IH,
    IW,
    captcha_vector,
    decode_png,
    ink_map,
    png_from_data_uri,
)

REFERENCE = Path(__file__).parent / "fixtures" / "captcha_reference.json"
CORPUS = Path(__file__).resolve().parents[1] / "data" / "captcha-training" / "solved"


@pytest.fixture(scope="module")
def reference() -> dict[str, list[float]]:
    if not REFERENCE.exists():  # pragma: no cover — эталон лежит в репозитории
        pytest.skip("нет эталона от JS-оригинала")
    return json.loads(REFERENCE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def samples(reference) -> dict[str, bytes]:
    if not CORPUS.exists():  # pragma: no cover — корпус не в git
        pytest.skip("корпус капч недоступен")
    found = {name: (CORPUS / name).read_bytes() for name in reference if (CORPUS / name).exists()}
    if not found:
        pytest.skip("ни одной картинки из эталона нет в корпусе")
    return found


def test_preprocessing_matches_the_original_exactly(reference, samples) -> None:
    """Ноль расхождения, а не «в пределах погрешности».

    Вход сети — усреднение целочисленной маски по целочисленной сетке,
    поэтому совпадать он обязан точно. Любое отличие означало бы, что сеть
    получает не то, на чём обучена.
    """
    worst = 0.0
    for name, blob in samples.items():
        got = captcha_vector(blob).reshape(-1)
        expected = np.asarray(reference[name], dtype=np.float32)
        worst = max(worst, float(np.abs(got - expected).max()))
    assert worst == 0.0


def test_vector_shape_and_range(samples) -> None:
    vector = captcha_vector(next(iter(samples.values())))
    assert vector.shape == (IH, IW)
    assert vector.dtype == np.float32
    # Доля «чернильных» пикселей в ячейке — от нуля до единицы.
    assert float(vector.min()) >= 0.0 and float(vector.max()) <= 1.0


def test_ink_map_finds_digits_but_not_background(samples) -> None:
    """Цифры ГАС сине-teal на почти белом фоне: чернил должно быть заметно
    меньше половины, но не ноль."""
    image = decode_png(next(iter(samples.values())))
    share = float(ink_map(image).mean())
    assert 0.02 < share < 0.5


def test_only_the_portal_png_format_is_accepted() -> None:
    """Другой формат — ошибка, а не повод угадывать."""
    with pytest.raises(ValueError, match="не PNG"):
        decode_png(b"\x00\x01\x02")


def test_data_uri_is_unwrapped() -> None:
    """Картинка приходит инлайном в форме поиска, с пробелами внутри base64."""
    import base64

    payload = b"\x89PNG\r\n\x1a\n"
    uri = "data:image/png;base64, " + base64.b64encode(payload).decode() + "\n"
    assert png_from_data_uri(uri) == payload
