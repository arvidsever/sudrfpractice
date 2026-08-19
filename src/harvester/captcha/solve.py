"""Прямой проход сети и решение капчи.

Порядок операций повторяет `cnnForward` из `sudtudtestbot`:

    вход − 0.5 → conv3×3(1→8) → leakyReLU → maxpool2
                → conv3×3(8→16) → leakyReLU → maxpool2
                → dense64 → leakyReLU → 5 голов softmax(10)

TTA — усреднение softmax по семи малым сдвигам входа. Это те же сдвиги,
которыми аугментировали обучение, поэтому усреднение не «размывает»
ответ, а снимает ложную сверхуверенность.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .model import ARCH, CaptchaModel

#: Сдвиги TTA — ровно те же, что в аугментации обучения (±2 по x, ±1 по y).
TTA_SHIFTS: tuple[tuple[int, int], ...] = (
    (0, 0),
    (1, 0),
    (-1, 0),
    (2, 0),
    (-2, 0),
    (0, 1),
    (0, -1),
)


@dataclass(frozen=True, slots=True)
class Solution:
    text: str
    #: Уверенность = минимум по головам: капча верна только целиком.
    confidence: float
    per_digit: tuple[float, ...]


def _conv(inp: np.ndarray, weight: np.ndarray, bias: np.ndarray) -> np.ndarray:
    """Свёртка 3×3 с нулевым паддингом, как `convF`."""
    channels_out, _, k, _ = weight.shape
    _, height, width = inp.shape
    pad = ARCH["PAD"]
    padded = np.pad(inp, ((0, 0), (pad, pad), (pad, pad)))

    out = (
        np.broadcast_to(bias[:, None, None], (channels_out, height, width))
        .astype(np.float32)
        .copy()
    )
    for ky in range(k):
        for kx in range(k):
            patch = padded[:, ky : ky + height, kx : kx + width]
            out += np.tensordot(weight[:, :, ky, kx], patch, axes=([1], [0]))
    return out


def _leaky(a: np.ndarray) -> np.ndarray:
    return np.where(a > 0, a, ARCH["LK"] * a).astype(np.float32)


def _maxpool2(a: np.ndarray) -> np.ndarray:
    channels, height, width = a.shape
    return a.reshape(channels, height // 2, 2, width // 2, 2).max(axis=(2, 4))


def forward(model: CaptchaModel, vector: np.ndarray) -> np.ndarray:
    """Логиты (HEADS × NC) для входа (IH, IW)."""
    x = (vector.astype(np.float32) - 0.5)[None, :, :]

    x = _maxpool2(_leaky(_conv(x, model.w1, model.b1)))
    x = _maxpool2(_leaky(_conv(x, model.w2, model.b2)))

    flat = x.reshape(-1)
    hidden = _leaky(model.wd @ flat + model.bd)
    return (model.wh @ hidden + model.bh).astype(np.float32)


def _softmax(logits: np.ndarray) -> np.ndarray:
    heads, classes = ARCH["HEADS"], ARCH["NC"]
    values = logits.reshape(heads, classes)
    values = values - values.max(axis=1, keepdims=True)
    exponent = np.exp(values)
    return (exponent / exponent.sum(axis=1, keepdims=True)).astype(np.float32)


def probabilities(model: CaptchaModel, vector: np.ndarray, tta: bool = True) -> np.ndarray:
    """Усреднённые вероятности (HEADS, NC)."""
    shifts = TTA_SHIFTS if tta else ((0, 0),)
    total = np.zeros((ARCH["HEADS"], ARCH["NC"]), dtype=np.float32)
    for dx, dy in shifts:
        shifted = vector if (dx == 0 and dy == 0) else _shift(vector, dx, dy)
        total += _softmax(forward(model, shifted)) / len(shifts)
    return total


def _shift(vector: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """Сдвиг входа с обнулением освободившегося края — как `shiftVec`."""
    out = np.zeros_like(vector)
    height, width = vector.shape
    src_y0, dst_y0 = max(0, -dy), max(0, dy)
    src_x0, dst_x0 = max(0, -dx), max(0, dx)
    rows, cols = height - abs(dy), width - abs(dx)
    if rows > 0 and cols > 0:
        out[dst_y0 : dst_y0 + rows, dst_x0 : dst_x0 + cols] = vector[
            src_y0 : src_y0 + rows, src_x0 : src_x0 + cols
        ]
    return out


def read_captcha(model: CaptchaModel, vector: np.ndarray, tta: bool = True) -> Solution:
    """Распознать капчу. Уверенность — минимум по головам."""
    probs = probabilities(model, vector, tta)
    digits = probs.argmax(axis=1)
    per_digit = probs.max(axis=1)
    return Solution(
        text="".join(str(int(d)) for d in digits),
        confidence=float(per_digit.min()),
        per_digit=tuple(float(p) for p in per_digit),
    )


def candidates(
    model: CaptchaModel, vector: np.ndarray, limit: int = 4, tta: bool = True
) -> list[tuple[str, float]]:
    """Варианты прочтения по убыванию правдоподобия.

    Портал держит одну картинку на IP несколько минут и НЕ меняет её
    от неверных ответов — проверено: три захода за формой подряд дают тот же
    `captchaid` и те же байты. Значит переспрашивать модель по новой форме
    бесполезно, а перебирать надо прочтения одной и той же картинки.

    Ошибается модель почти всегда в одной цифре, поэтому кандидаты строятся
    заменой ОДНОЙ цифры на второй по вероятности вариант, начиная с той
    головы, где модель сомневалась сильнее всего. Отношение `p2 / p1` и есть
    цена такой замены.
    """
    probs = probabilities(model, vector, tta)
    order = np.argsort(-probs, axis=1)
    best = "".join(str(int(d)) for d in order[:, 0])
    joint = float(probs[np.arange(len(order)), order[:, 0]].min())

    result = [(best, joint)]
    swaps = []
    for head in range(probs.shape[0]):
        first, second = order[head, 0], order[head, 1]
        p1, p2 = probs[head, first], probs[head, second]
        if p1 > 0:
            swaps.append((float(p2 / p1), head, int(second)))

    for ratio, head, digit in sorted(swaps, reverse=True)[: max(0, limit - 1)]:
        text = best[:head] + str(digit) + best[head + 1 :]
        result.append((text, joint * ratio))
    return result
