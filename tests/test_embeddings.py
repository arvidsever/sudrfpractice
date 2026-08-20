"""Эмбеддинги: то, что ломается молча.

Модель тут не грузится — она весит гигабайт и считает минутами. Проверяется
обвязка: формат вектора для pgvector и нарезка, которая не должна ни
задваивать куски, ни трогать уже нарезанное.
"""

from __future__ import annotations

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy import text as sql

from harvester.embeddings import as_literal, split_pending


def test_vector_literal_is_what_pgvector_eats() -> None:
    assert as_literal(np.array([1.0, -0.5, 0.0])) == "[1.000000,-0.500000,0.000000]"


def _seed_act(engine, body: str) -> None:
    with engine.begin() as c:
        case_pk = c.execute(
            sql("""INSERT INTO "case" (court_domain, cartoteka_id, case_number, case_uid)
                   VALUES ('5kas.sudrf.ru', 'g3', '88-1/2026', 'uid-1') RETURNING id""")
        ).scalar_one()
        act_id = c.execute(
            sql("INSERT INTO act (case_pk, text_number) VALUES (:c, 1) RETURNING id"),
            {"c": case_pk},
        ).scalar_one()
        c.execute(
            sql("INSERT INTO act_text (act_id, plain_text) VALUES (:a, :t)"),
            {"a": act_id, "t": body},
        )


def test_splitting_is_not_repeated(db_settings) -> None:
    """Второй заход не должен задваивать куски: иначе каждый прогон
    удваивал бы работу счёта, а поиск отдавал бы одно и то же дважды."""
    engine = create_engine(db_settings.database_url)
    _seed_act(engine, "\n".join(["Мотивировочная часть определения. " * 30] * 4))

    first = split_pending(engine)
    second = split_pending(engine)

    with engine.connect() as c:
        total = c.execute(sql("SELECT count(*) FROM chunk")).scalar_one()
    engine.dispose()

    assert first > 1, "текст должен резаться больше чем на один кусок"
    assert second == 0, "акт уже нарезан — второй раз браться за него незачем"
    assert total == first
