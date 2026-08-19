"""Сверка питоновского порта распознавателя с оригиналом на JS.

Первый уровень — препроцессинг — закрыт тестом и обязан совпадать точно.
Здесь второй: вероятности после прямого прохода. Побитового совпадения тут
не требуется и не бывает — порядок суммирования в свёртке и в полносвязном
слое у NumPy другой, чем у последовательного цикла на JS. Значение имеет
другое: одинаковые ЦИФРЫ на всех картинках и вероятности, расходящиеся
на уровне ошибки float32.

Запуск:
    CAPTCHA_MODEL=data/captcha-model.json \
      node scripts/dump-captcha-probs.mjs <sudtudtestbot> <корпус> 200 > /tmp/probs_js.json
    python scripts/compare_captcha_port.py /tmp/probs_js.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harvester.captcha.model import load_model  # noqa: E402
from harvester.captcha.preprocess import captcha_vector  # noqa: E402
from harvester.captcha.solve import probabilities  # noqa: E402

CORPUS = Path(__file__).resolve().parents[1] / "data" / "captcha-training" / "solved"


def main(reference_path: str) -> int:
    reference = json.loads(Path(reference_path).read_text(encoding="utf-8"))
    model = load_model()

    worst = {"tta": 0.0, "single": 0.0}
    digit_mismatch = 0
    correct_vs_label = 0
    checked = 0

    for name, expected in reference.items():
        blob = (CORPUS / name).read_bytes()
        vector = captcha_vector(blob)
        checked += 1

        for mode, tta in (("tta", True), ("single", False)):
            got = probabilities(model, vector, tta=tta)
            want = np.asarray(expected[mode], dtype=np.float32).reshape(got.shape)
            worst[mode] = max(worst[mode], float(np.abs(got - want).max()))
            if mode == "tta":
                if not np.array_equal(got.argmax(axis=1), want.argmax(axis=1)):
                    digit_mismatch += 1
                    print(f"  РАСХОЖДЕНИЕ ЦИФР: {name}")
                predicted = "".join(str(int(d)) for d in got.argmax(axis=1))
                if predicted == name.split("_", 1)[0]:
                    correct_vs_label += 1

    print(f"сверено картинок: {checked}")
    print(
        f"максимальное расхождение вероятностей: TTA {worst['tta']:.3e}, "
        f"один проход {worst['single']:.3e}"
    )
    print(f"картинок с разными цифрами: {digit_mismatch}")
    print(
        f"капча целиком верна против метки: {correct_vs_label}/{checked} "
        f"({100 * correct_vs_label / checked:.1f}%)"
    )

    ok = digit_mismatch == 0 and worst["tta"] < 1e-4
    print("ПОРТ СОВПАДАЕТ" if ok else "ПОРТ РАСХОДИТСЯ")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/probs_js.json"))
