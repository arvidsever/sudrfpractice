"""Разбор карточки дела.

Главное, что здесь проверяется: карточка устроена по-разному в зависимости
от вида производства, и парсер, знающий только гражданскую, по уголовным
и КоАП вернул бы пустой список участников — молча, со сходящимися
счётчиками.
"""

from __future__ import annotations

from harvester.parse.card import parse_card


def test_civil_card(case_card: str) -> None:
    card = parse_card(case_card)

    assert card.case_uid == "77RS0020-02-2025-007285-88"
    assert card.judge == "Попова Елена Викторовна"
    assert card.decision_date == "14.05.2026"
    assert card.appeal_result == "Без изменения"

    # Связка с первой инстанцией отдельными полями, а не строкой в колонке.
    assert card.lower_court is not None
    assert card.lower_court.court == "Перовский районный суд (Город Москва)"
    assert card.lower_court.case_number == "2-5462/2025"
    assert card.lower_court.decision_date == "19.08.2025"

    assert [(p.role, p.name) for p in card.participants] == [
        ("ИСТЕЦ", "ООО ПКО Шамиль и партнеры"),
        ("ОТВЕТЧИК", "Симкин Георгий Александрович"),
    ]


def test_hearing_columns_are_not_shifted(case_card: str) -> None:
    """Колонок в шапке восемь, и заголовок последней склеен с подсказкой.
    Сравнение по началу заголовка, а не по равенству, — не придирка:
    иначе дата размещения потерялась бы, а зал попал бы во время."""
    hearing = parse_card(case_card).hearings[0]

    assert hearing.event == "Судебное заседание"
    assert (hearing.date, hearing.time, hearing.place) == ("14.05.2026", "11:25", "505")
    assert hearing.published_at == "29.04.2026"


def test_criminal_card_has_articles(card_criminal: str) -> None:
    """У уголовных участники лежат в ДВУХ таблицах: «СТОРОНЫ» и «ЛИЦА».
    Перечень статей есть только во второй — за нормами не нужно ходить
    во внешний источник."""
    card = parse_card(card_criminal)

    roles = [p.role for p in card.participants]
    assert "Прокурор" in roles
    assert "ЛИЦО" in roles

    charged = [p for p in card.participants if p.articles]
    assert len(charged) == 1
    assert charged[0].articles == "ст.183 ч.4; ст.290 ч.3 УК РФ"
    assert charged[0].outcome


def test_criminal_card_has_appeals(card_criminal: str) -> None:
    """У кассации жалобу сперва изучает судья, и до заседания доходит
    не всякая — этот путь виден в таблице «ЖАЛОБЫ»."""
    appeals = parse_card(card_criminal).appeals

    assert len(appeals) == 2
    assert {a.applicant_status for a in appeals} == {"АДВОКАТОМ", "ПРОКУРОРОМ"}
    assert all(a.filed_at for a in appeals)


def test_koap_card_has_articles_in_its_own_table(card_koap: str) -> None:
    """У КоАП таблица называется «СТОРОНЫ ПО ДЕЛУ», и статья лежит в ней же."""
    card = parse_card(card_koap)

    assert len(card.participants) == 1
    person = card.participants[0]
    assert person.role == "ПРИВЛЕКАЕМОЕ ЛИЦО"
    assert person.articles == "ст.12.15 ч.4 КоАП РФ"


def test_missing_sections_are_not_a_failure(card_koap: str) -> None:
    """У КоАП слушаний и жалоб в карточке может не быть вовсе."""
    card = parse_card(card_koap)

    assert card.hearings == []
    assert card.appeals == []
    assert card.case_uid


def test_every_card_carries_its_act_text(
    case_card: str, card_criminal: str, card_koap: str
) -> None:
    """Ради этого свод и переехал с `name_op=doc` на карточку: тот же один
    запрос, но сверх текста — участники, статьи и движение."""
    for html in (case_card, card_criminal, card_koap):
        card = parse_card(html)
        assert card.has_act_text
        assert len(card.act_texts[1]) > 1000
