"""Загрузка весов CNN из JSON, обученного скриптом `sudtudtestbot`.

Архитектура зашита в самом файле весов (поле `arch`) — сверяемся с ней,
а не полагаемся на константы: молчаливое расхождение формы дало бы
не ошибку, а тихо неверные цифры.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: Архитектура, на которую рассчитан этот порт.
ARCH = {
    "IW": 64,
    "IH": 20,
    "C1": 8,
    "C2": 16,
    "K": 3,
    "PAD": 1,
    "HID": 64,
    "HEADS": 5,
    "NC": 10,
    "LK": 0.1,
}

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "data" / "captcha-model.json"


@dataclass(frozen=True, slots=True)
class CaptchaModel:
    w1: np.ndarray  # (C1, 1, K, K)
    b1: np.ndarray  # (C1,)
    w2: np.ndarray  # (C2, C1, K, K)
    b2: np.ndarray  # (C2,)
    wd: np.ndarray  # (HID, C2 * 5 * 16)
    bd: np.ndarray  # (HID,)
    wh: np.ndarray  # (HEADS * NC, HID)
    bh: np.ndarray  # (HEADS * NC,)


def load_model(path: str | Path | None = None) -> CaptchaModel:
    raw = json.loads(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))

    arch = raw.get("arch", {})
    mismatched = {k: (v, arch.get(k)) for k, v in ARCH.items() if arch.get(k) != v}
    if mismatched:
        raise ValueError(f"архитектура весов разошлась с портом: {mismatched}")

    c1, c2, k = ARCH["C1"], ARCH["C2"], ARCH["K"]
    hid, heads, nc = ARCH["HID"], ARCH["HEADS"], ARCH["NC"]
    flat = c2 * (ARCH["IH"] // 4) * (ARCH["IW"] // 4)

    def arr(name: str, shape: tuple[int, ...]) -> np.ndarray:
        values = np.asarray(raw[name], dtype=np.float32)
        if values.size != int(np.prod(shape)):
            raise ValueError(
                f"{name}: ожидалось {int(np.prod(shape))} чисел, в файле {values.size}"
            )
        return values.reshape(shape)

    return CaptchaModel(
        w1=arr("w1", (c1, 1, k, k)),
        b1=arr("b1", (c1,)),
        w2=arr("w2", (c2, c1, k, k)),
        b2=arr("b2", (c2,)),
        wd=arr("wd", (hid, flat)),
        bd=arr("bd", (hid,)),
        wh=arr("wh", (heads * nc, hid)),
        bh=arr("bh", (heads * nc,)),
    )
