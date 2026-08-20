"""Эмбеддинги кусков актов на MLX.

MLX выбран замером: 10 860 токенов в секунду против 4 248 у PyTorch
на MPS — он написан под Apple Silicon, а не переведён на него.

Два подводных камня, оба уже наступленных:

* **MLX считает лениво.** Без `mx.eval` возвращается обещание, а не
  результат: первый замер показал ускорение в 480 раз, потому что мерил
  составление списка дел;
* **MLX ставится только на Apple Silicon**, поэтому импортируется внутри
  `embed()`, а не на уровне модуля: иначе от него зависел бы сам импорт
  `harvester.embeddings`, и на линуксовом CI не собирались бы даже тесты;
* **векторы MLX и PyTorch не взаимозаменяемы.** Косинус между путями
  0,996 по медиане, но у пятой части кусков расходится ближайший сосед.
  Значит путь один и на документы, и на запросы — отсюда `embed()`
  единственная точка входа для обоих.
"""

from __future__ import annotations

import logging

import numpy as np
from sqlalchemy import Engine
from sqlalchemy import text as sql

from .chunking import chunk_act

log = logging.getLogger("harvester.embeddings")

MODEL = "Qwen/Qwen3-Embedding-0.6B"

#: Замерено: 64 медленнее 32, и 128 тоже.
BATCH = 32

_model = None
_tokenizer = None


def _load():
    global _model, _tokenizer
    if _model is None:
        from mlx_embeddings import load

        log.info("загружаю %s", MODEL)
        _model, _tokenizer = load(MODEL)
    return _model, _tokenizer


def embed(texts: list[str]) -> np.ndarray:
    """Единая точка входа: и документы, и запросы считаются здесь."""
    import mlx.core as mx
    from mlx_embeddings import generate

    model, tokenizer = _load()
    out = []
    for start in range(0, len(texts), BATCH):
        vectors = generate(model, tokenizer, texts=texts[start : start + BATCH]).text_embeds
        mx.eval(vectors)  # без этого вернётся обещание, а не числа
        out.append(np.array(vectors.astype(mx.float32), dtype=np.float32))

    stacked = np.vstack(out)
    return stacked / np.linalg.norm(stacked, axis=1, keepdims=True)


def as_literal(vector: np.ndarray) -> str:
    """Вектор в том виде, в каком его принимает pgvector."""
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


def split_pending(engine: Engine, *, limit: int | None = None) -> int:
    """Нарезать акты, у которых кусков ещё нет. Возвращает число кусков."""
    with engine.begin() as connection:
        rows = connection.execute(
            sql("""
                SELECT a.id, t.plain_text
                FROM act a JOIN act_text t ON t.act_id = a.id
                WHERE NOT EXISTS (SELECT 1 FROM chunk c WHERE c.act_id = a.id)
                LIMIT :limit
            """),
            {"limit": limit},
        ).all()

        made = 0
        for act_id, body in rows:
            for piece in chunk_act(body):
                connection.execute(
                    sql("INSERT INTO chunk (act_id, ordinal, text) VALUES (:a, :o, :t)"),
                    {"a": act_id, "o": piece.ordinal, "t": piece.text},
                )
                made += 1
    return made


def fill_embeddings(engine: Engine, *, limit: int | None = None) -> int:
    """Посчитать эмбеддинги для кусков, у которых их нет."""
    with engine.begin() as connection:
        rows = connection.execute(
            sql("SELECT id, text FROM chunk WHERE embedding IS NULL LIMIT :limit"),
            {"limit": limit},
        ).all()
        if not rows:
            return 0

        vectors = embed([body for _, body in rows])
        for (chunk_id, _), vector in zip(rows, vectors, strict=True):
            connection.execute(
                sql("UPDATE chunk SET embedding = :v WHERE id = :id"),
                {"v": as_literal(vector), "id": chunk_id},
            )
    return len(rows)
