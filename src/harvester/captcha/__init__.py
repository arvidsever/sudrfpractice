"""Распознавание капчи ГАС «Правосудие».

Порт распознавателя из `sudtudtestbot` (использование разрешено автором).
Веса обучаются его же скриптом на JS; здесь только инференс на NumPy —
пять слоёв читаются из JSON напрямую, промежуточные форматы вроде ONNX
не нужны.

Порт обязан совпадать с оригиналом, а не «работать похоже»: препроцессинг
несёт большую часть точности, и молчаливое расхождение в нём выглядело бы
как плохая модель. Сверка — `tests/test_captcha.py`.
"""

from .model import CaptchaModel, load_model
from .preprocess import captcha_vector, decode_png
from .solve import candidates, read_captcha

# Имя `solve` здесь не экспортируется намеренно: оно перекрывало бы
# одноимённый подмодуль, и `harvester.captcha.solve` означало бы функцию.
__all__ = [
    "CaptchaModel",
    "candidates",
    "captcha_vector",
    "decode_png",
    "load_model",
    "read_captcha",
]
