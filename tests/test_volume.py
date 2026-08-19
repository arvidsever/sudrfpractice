"""Замер объёма картотек.

Замер обязан честно различать «столько-то дел» и «не смогли посмотреть».
Ноль в этой таблице означал бы пустую картотеку, а капча и блокировка —
это не ноль, это отсутствие ответа.
"""

from __future__ import annotations

from datetime import date

import pytest

from harvester.directories import cartoteka, court
from harvester.urls import whole_cartoteka_url
from harvester.volume import measure_pair


class _Client:
    def __init__(self, html: str | None = None, error: Exception | None = None):
        self.html, self.error, self.urls = html, error, []

    def get_passing_captcha(self, url: str, attempts: int = 3):
        """Замер идёт тем же путём, что обход: на капча-судах через решатель."""
        return self.get(url)

    def get(self, url: str):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        from harvester.http import Response

        return Response(url=url, status_code=200, content=self.html.encode("cp1251", "replace"))


class _SequenceClient(_Client):
    """Отдаёт заготовленные страницы по порядку — для проверки второй попытки."""

    def __init__(self, *pages: str):
        super().__init__(pages[0])
        self.pages = list(pages)

    def get(self, url: str):
        self.urls.append(url)
        from harvester.http import Response

        page = self.pages.pop(0) if self.pages else self.pages
        return Response(url=url, status_code=200, content=page.encode("cp1251", "replace"))


COURT = court("2kas.sudrf.ru")
CARTOTEKA = cartoteka("g3")


def test_counter_becomes_the_volume(listing_acts: str) -> None:
    result = measure_pair(_Client(listing_acts), COURT, CARTOTEKA)
    assert (result.status, result.total_cases) == ("measured", 530)


def test_captcha_is_not_zero(listing_captcha_gate: str) -> None:
    """Капча-суд не измерен, а не пуст. Ноль здесь стал бы ложью в плане обхода."""
    result = measure_pair(_Client(listing_captcha_gate), COURT, CARTOTEKA)
    assert result.status == "failed"
    assert result.total_cases is None


def test_throttling_is_marked_separately(temporarily_unavailable: str) -> None:
    """Придержанный суд можно перемерить позже — это отличается от отказа.

    «Временно недоступна» и на запросе без дат, и на запросе с окном —
    значит дело не в цене запроса, и пара остаётся незамеренной.
    """
    client = _SequenceClient(temporarily_unavailable, temporarily_unavailable)
    result = measure_pair(client, COURT, CARTOTEKA, today=date(2026, 8, 20))

    assert result.status == "throttled"
    assert result.total_cases is None
    assert len(client.urls) == 2, "первый же кусок не дался — дальше спрашивать незачем"


def test_heavy_query_falls_back_to_a_date_window(
    temporarily_unavailable: str, listing_acts: str
) -> None:
    """Запрос ко всей картотеке — самый дорогой из возможных, и на больших
    картотеках суд его не тянет: отвечает «Информация временно недоступна»,
    той же страницей, какой просит отступить. Отличить по странице нельзя,
    а переспросить с окном дат — можно. Так закрылась пара `3kas/g3`.
    """
    client = _SequenceClient(temporarily_unavailable, *([listing_acts] * 3))
    result = measure_pair(client, COURT, CARTOTEKA, today=date(2026, 8, 20))

    assert result.status == "measured"
    assert result.total_cases == 530 * 3, "счётчики кусков складываются"
    assert "кусков" in (result.note or ""), "пометка нужна: число получено иначе, чем у соседей"

    first, *chunks = client.urls
    assert "DATE1D" not in first, "первым идёт запрос ко всей картотеке"
    assert len(chunks) == 3
    assert "ENTRY_DATE1D=01.10.2019" in chunks[0]
    assert "ENTRY_DATE2D=20.08.2026" in chunks[-1]


def test_depth_chunks_do_not_overlap_or_leave_gaps() -> None:
    """Куски складываются в число, поэтому стык между ними обязан быть ровно
    один день: нахлёст завысит сумму, дыра занизит — и оба молча."""
    from harvester.volume import split_depth

    chunks = split_depth(date(2019, 10, 1), date(2026, 8, 20))

    assert chunks[0][0] == date(2019, 10, 1)
    assert chunks[-1][1] == date(2026, 8, 20)
    for (_, before), (after, _) in zip(chunks, chunks[1:], strict=False):
        assert (after - before).days == 1, f"стык {before} → {after} не встык"


def test_empty_answer_is_not_counted(listing_bad_new: str) -> None:
    """Пустая картотека и кривой запрос по тексту неотличимы, поэтому это
    не «нуль дел», а «нечего засчитывать»."""
    result = measure_pair(_Client(listing_bad_new), COURT, CARTOTEKA)
    assert result.status == "empty"
    assert result.total_cases is None


def test_network_failure_keeps_the_pair_unmeasured() -> None:
    result = measure_pair(_Client(error=RuntimeError("сеть")), COURT, CARTOTEKA)
    assert result.status == "failed"
    assert "RuntimeError" in (result.note or "")


def test_volume_url_carries_no_date_filter() -> None:
    """Счётчик по всей картотеке даёт только запрос без окна."""
    url = whole_cartoteka_url(COURT, CARTOTEKA)
    assert "DATE1D" not in url
    assert "PUBL_DATE" not in url
    # Длинный delo_id обязателен и здесь: иначе счётчик тот же, но выдача чужая.
    assert "&delo_id=2800001&" in url

    with pytest.raises(ValueError):
        whole_cartoteka_url(COURT, CARTOTEKA, page=0)


def test_measurement_keeps_the_page_it_judged(listing_acts: str, tmp_path) -> None:
    """Замер обращается к суду — значит обязан оставить сырьё.

    Вердикт `throttled` ставится по фразе, которую ищут по всей странице.
    Без сохранённых байтов заглушку и обычную страницу с той же фразой
    в вёрстке нельзя различить иначе, чем ещё одним запросом к суду.
    """
    from harvester.raw import RawStore

    store = RawStore(tmp_path)
    result = measure_pair(_Client(listing_acts), COURT, CARTOTEKA, store)

    assert result.raw is not None
    assert result.raw.content_kind == "volume"
    assert (tmp_path / result.raw.path).exists()
