"""Препроцессинг капчи: PNG → карта «чернильности» 64×20.

Повторяет `server/captcha.js` из `sudtudtestbot` шаг в шаг. Отклоняться
здесь нельзя: сеть обучена ровно на этом представлении, и любая вольность
в фильтре цвета или в усреднении бьёт по точности молча.

Декодер PNG свой, а не через библиотеку изображений: портал отдаёт ровно
один формат (colorType 2, 8 бит, без интерлейса), а лишняя зависимость
ради сорока строк того не стоит.
"""

from __future__ import annotations

import base64
import re
import zlib
from dataclasses import dataclass

import numpy as np

#: Вход сети: 64 столбца × 20 строк «чернильности».
IW, IH = 64, 20

#: Цвет цифр ГАС постоянен — сине-teal.
TEAL = (2, 103, 154)

_DATA_URI = re.compile(r"^data:\s*image/[a-z]+;\s*base64,\s*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Image:
    width: int
    height: int
    rgb: np.ndarray  # (h, w, 3), uint8


def png_from_data_uri(uri: str) -> bytes:
    """Картинка приходит инлайном в форме поиска — отдельный запрос не нужен."""
    return base64.b64decode(re.sub(r"\s+", "", _DATA_URI.sub("", uri or "")))


def decode_png(buf: bytes) -> Image:
    """Разобрать PNG портала. Другой формат — ошибка, а не повод угадывать."""
    if len(buf) < 8 or buf[0] != 0x89 or buf[1] != 0x50:
        raise ValueError("не PNG")

    pos, width, height, color_type = 8, 0, 0, 2
    idat = bytearray()
    while pos < len(buf):
        length = int.from_bytes(buf[pos : pos + 4], "big")
        kind = buf[pos + 4 : pos + 8]
        if kind == b"IHDR":
            width = int.from_bytes(buf[pos + 8 : pos + 12], "big")
            height = int.from_bytes(buf[pos + 12 : pos + 16], "big")
            color_type = buf[pos + 17]
        elif kind == b"IDAT":
            idat += buf[pos + 8 : pos + 8 + length]
        elif kind == b"IEND":
            break
        pos += 12 + length

    if color_type != 2:
        raise ValueError(f"неподдерживаемый colorType {color_type}")

    data = zlib.decompress(bytes(idat))
    bpp, stride = 3, width * 3
    out = bytearray(height * stride)

    for y in range(height):
        filter_type = data[y * (stride + 1)]
        row = y * (stride + 1) + 1
        base = y * stride
        prev = base - stride
        for i in range(stride):
            raw = data[row + i]
            a = out[base + i - bpp] if i >= bpp else 0
            b = out[prev + i] if y > 0 else 0
            c = out[prev + i - bpp] if (y > 0 and i >= bpp) else 0
            if filter_type == 1:
                value = raw + a
            elif filter_type == 2:
                value = raw + b
            elif filter_type == 3:
                value = raw + ((a + b) >> 1)
            elif filter_type == 4:
                value = raw + _paeth(a, b, c)
            else:
                value = raw
            out[base + i] = value & 255

    rgb = np.frombuffer(bytes(out), dtype=np.uint8).reshape(height, width, 3)
    return Image(width=width, height=height, rgb=rgb)


def _paeth(a: int, b: int, c: int) -> int:
    q = a + b - c
    pa, pb, pc = abs(q - a), abs(q - b), abs(q - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def ink_map(image: Image) -> np.ndarray:
    """Единицы там, где пиксель цвета цифр.

    Два условия через ИЛИ: близость к teal (ядро и осветлённые края)
    либо явная сине-доминантность. Белым считается всё, у чего минимальный
    канал не меньше 205.
    """
    rgb = image.rgb.astype(np.int16)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]

    white = rgb.min(axis=-1) >= 205
    near_teal = (np.abs(r - TEAL[0]) + np.abs(g - TEAL[1]) + np.abs(b - TEAL[2])) <= 95
    bluish = (b > r + 45) & (b >= g - 8) & (b > 70) & (b < 210) & (r < 150)

    return (~white & (near_teal | bluish)).astype(np.uint8)


def pool_ink(ink: np.ndarray) -> np.ndarray:
    """Усреднить чернильность из родного размера в сетку 64×20.

    Границы ячеек считаются нацело, как в оригинале: при 100×30 столбцы
    выходят неравной ширины, и «честное» дробное усреднение дало бы
    другой вход, чем тот, на котором сеть обучена.
    """
    height, width = ink.shape
    xs = [(x * width // IW, max(x * width // IW + 1, (x + 1) * width // IW)) for x in range(IW)]
    ys = [(y * height // IH, max(y * height // IH + 1, (y + 1) * height // IH)) for y in range(IH)]

    vector = np.empty((IH, IW), dtype=np.float32)
    integral = np.zeros((height + 1, width + 1), dtype=np.int32)
    integral[1:, 1:] = ink.cumsum(axis=0).cumsum(axis=1)

    for ty, (y0, y1) in enumerate(ys):
        for tx, (x0, x1) in enumerate(xs):
            total = integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
            vector[ty, tx] = total / ((y1 - y0) * (x1 - x0))
    return vector


def captcha_vector(buf: bytes) -> np.ndarray:
    """PNG → вход сети (20, 64)."""
    return pool_ink(ink_map(decode_png(buf)))
