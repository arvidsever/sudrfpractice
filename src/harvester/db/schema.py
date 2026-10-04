"""Схема правовой базы.

Что схема обязана удерживать, кроме данных:

* **провенанс** — у каждого текста есть сырая страница, из которой он получен;
* **полноту** — `harvest_run` хранит обещание счётчика и то, сколько строк
  реально разобрано. Недосбор не проявляется как ошибка, и только этот
  журнал делает его видимым;
* **честность про 262-ФЗ** — «текст не опубликован» это явное состояние дела,
  а не отсутствие строки в `act`. Иначе база выглядит полнее, чем есть.

Колонки `embedding` здесь нет намеренно: `vector(N)` фиксирует размерность
схемой, а модель эмбеддингов ещё не выбрана. Добавить колонку миграцией
дешевле, чем менять размерность.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

metadata = MetaData()

court = Table(
    "court",
    metadata,
    Column("domain", String(64), primary_key=True),
    Column("number", Integer, nullable=True, comment="номер КСОЮ; пуст у военного суда"),
    Column("title", Text, nullable=False),
    Column("level", String(16), nullable=False),
    Column("regions", Text, nullable=False, comment="регионы подсудности, JSON-массив"),
    Column("has_captcha", Boolean, nullable=False, server_default="false"),
)

cartoteka = Table(
    "cartoteka",
    metadata,
    Column("id", String(16), primary_key=True),
    Column("title", Text, nullable=False),
    Column("delo_id", String(16), nullable=False),
    Column("new", String(16), nullable=False, comment="важнее delo_id: new=0 отдаёт форму"),
    Column("delo_table", String(32), nullable=False),
    Column("doc_prefix", String(32), nullable=False),
)

raw_page = Table(
    "raw_page",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("sha256", String(64), nullable=False, unique=True),
    Column("url", Text, nullable=False),
    Column("court_domain", String(64), ForeignKey("court.domain"), nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("http_status", Integer, nullable=False),
    Column("byte_size", Integer, nullable=False),
    Column("content_kind", String(16), nullable=False, comment="listing | act | card"),
    Column("path", Text, nullable=False),
)

case = Table(
    "case",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("court_domain", String(64), ForeignKey("court.domain"), nullable=False),
    Column("cartoteka_id", String(16), ForeignKey("cartoteka.id"), nullable=False),
    Column("case_id", String(32), nullable=True, comment="case_id в базе суда"),
    Column("case_uid", String(64), nullable=True, comment="УИД дела"),
    Column("case_number", Text, nullable=False),
    Column("receipt_date", Date, nullable=True),
    Column("essence", Text, nullable=True),
    Column("judge", Text, nullable=True),
    Column("decision_date", Date, nullable=True),
    Column("result", Text, nullable=True),
    Column("legal_force_date", Date, nullable=True),
    Column("card_url", Text, nullable=True),
    Column(
        "act_published",
        Boolean,
        nullable=True,
        comment="true — акт есть; false — карточку открыли, текста нет (262-ФЗ); "
        "null — не проверяли. См. миграцию 0002",
    ),
    Column("category", Text, nullable=True),
    Column("appealed_act", Text, nullable=True),
    Column("appeal_result", Text, nullable=True),
    Column("lower_region", Text, nullable=True),
    Column("lower_court", Text, nullable=True),
    Column("lower_case_number", Text, nullable=True),
    Column("lower_decision_date", Date, nullable=True),
    Column("card_fetched_at", DateTime(timezone=True), nullable=True),
    Column("first_seen", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_seen", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("court_domain", "case_uid", name="uq_case_court_uid"),
)

act = Table(
    "act",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("case_pk", BigInteger, ForeignKey("case.id", ondelete="CASCADE"), nullable=False),
    Column(
        "doc_number",
        String(32),
        nullable=True,
        comment="number= из ссылки перечня; в карточке его нет",
    ),
    Column("text_number", Integer, nullable=False, server_default="1"),
    Column("kind", Text, nullable=True),
    Column("url", Text, nullable=True),
    Column("publ_date", Date, nullable=True),
    # Акт опознаётся номером вкладки: doc_number есть только у тех, кого
    # мы нашли через ссылку в перечне.
    UniqueConstraint("case_pk", "text_number", name="uq_act_case_text"),
)

act_text = Table(
    "act_text",
    metadata,
    Column("act_id", BigInteger, ForeignKey("act.id", ondelete="CASCADE"), primary_key=True),
    Column("raw_page_id", BigInteger, ForeignKey("raw_page.id"), nullable=True),
    Column("plain_text", Text, nullable=False),
    # tsv — generated-колонка, создаётся миграцией: SQLAlchemy Computed с tsvector
    # в alembic всё равно пишется руками, а держать её тут значило бы описать дважды.
)

#: Индексы под фасеты поиска создаёт миграция 0007, и `CONCURRENTLY` —
#: не украшение: сбор пишет в `case` круглосуточно. Здесь их нет намеренно,
#: иначе `metadata.create_all` строил бы их блокирующе.

harvest_run = Table(
    "harvest_run",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("court_domain", String(64), ForeignKey("court.domain"), nullable=False),
    Column("cartoteka_id", String(16), ForeignKey("cartoteka.id"), nullable=False),
    Column("axis", String(16), nullable=False, comment="entry | result | publication"),
    Column("window_from", Date, nullable=False),
    Column("window_to", Date, nullable=False),
    Column("expected_count", Integer, nullable=True, comment="счётчик «Всего найдено»"),
    Column("fetched_rows", Integer, nullable=False, server_default="0"),
    Column("pages_done", Integer, nullable=False, server_default="0"),
    Column(
        "status",
        String(16),
        nullable=False,
        comment="running | complete | empty | pilot | short | throttled | deferred | failed",
    ),
    Column("note", Text, nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
)


cartoteka_volume = Table(
    "cartoteka_volume",
    metadata,
    Column("court_domain", String(64), ForeignKey("court.domain"), primary_key=True),
    Column("cartoteka_id", String(16), ForeignKey("cartoteka.id"), primary_key=True),
    Column("total_cases", Integer, nullable=True, comment="счётчик без фильтра дат"),
    Column("status", String(16), nullable=False, comment="measured | empty | throttled | failed"),
    Column("note", Text, nullable=True),
    Column("measured_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

harvest_task = Table(
    "harvest_task",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("court_domain", String(64), ForeignKey("court.domain"), nullable=False),
    Column("cartoteka_id", String(16), ForeignKey("cartoteka.id"), nullable=False),
    Column("axis", String(16), nullable=False),
    Column("window_from", Date, nullable=False),
    Column("window_to", Date, nullable=False),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("cases_found", Integer, nullable=True),
    Column("run_id", BigInteger, ForeignKey("harvest_run.id"), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "court_domain", "cartoteka_id", "axis", "window_from", "window_to", name="uq_task_window"
    ),
)


participant = Table(
    "participant",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("case_pk", BigInteger, ForeignKey("case.id", ondelete="CASCADE"), nullable=False),
    Column("role", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("articles", Text, nullable=True, comment="перечень статей: уголовные и КоАП"),
    Column("outcome", Text, nullable=True, comment="результат в отношении лица: уголовные"),
    # Свободный текст, а не поле фиксированной длины: под заголовком «КПП»
    # суд может написать название организации, и портал отдаст как есть.
    # `varchar(16)` на такой строке уронил свод карточек на трое суток.
    Column("inn", Text, nullable=True),
    Column("kpp", Text, nullable=True),
    Column("ogrn", Text, nullable=True),
    Column("ogrnip", Text, nullable=True),
)

hearing = Table(
    "hearing",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("case_pk", BigInteger, ForeignKey("case.id", ondelete="CASCADE"), nullable=False),
    Column("event", Text, nullable=False),
    Column("hearing_date", Date, nullable=True),
    Column("hearing_time", Text, nullable=True),
    Column("place", Text, nullable=True),
    Column("result", Text, nullable=True),
    Column("published_at", Date, nullable=True),
)


appeal = Table(
    "appeal",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("case_pk", BigInteger, ForeignKey("case.id", ondelete="CASCADE"), nullable=False),
    Column("filed_at", Date, nullable=True),
    Column("applicant_status", Text, nullable=True),
    Column("applicant", Text, nullable=True),
    Column("passed_to_study_at", Date, nullable=True),
    Column("with_case_request", Text, nullable=True),
    Column("ruling_date", Date, nullable=True),
    Column("study_result", Text, nullable=True),
)


# --- ВС РФ (docs/vsrf.md) ---------------------------------------------------
#
# Своими таблицами, а не в `case`: производство ВС не ложится в дело
# с картотекой и `case_id`. Связь с нашим корпусом — по УИД
# (`vsrf_claim.case_uid` ↔ `case.case_uid`), а акт связан с производством
# идентификатором карточки (`vsrf_act.claim_id` ↔ `vsrf_claim.claim_id`).

vsrf_act = Table(
    "vsrf_act",
    metadata,
    #: Номер PDF в `/lk/practice/stor_pdf/{id}` — у акта он единственный.
    Column("pdf_id", BigInteger, primary_key=True, autoincrement=False),
    Column("number", Text, nullable=True, comment="номер производства ВС, напр. АПЛ26-207"),
    Column("claim_id", Text, nullable=True, comment="карточка: /lk/practice/claims/{id}"),
    Column("act_kind", Text, nullable=True, comment="Определение, Постановление…"),
    Column("act_date", Date, nullable=True),
    Column("case_type", String(32), nullable=False, comment="CIVIL, CRIMINAL… — фильтр выдачи"),
    Column("instance", Text, nullable=True),
    Column("subject", Text, nullable=True),
    Column("collegium", Text, nullable=True),
    Column("judge", Text, nullable=True),
    Column("first_seen", DateTime(timezone=True), nullable=False, server_default=func.now()),
    #: NULL — PDF ещё не брали. Очередь текстов — это и есть `IS NULL`.
    Column("text_fetched_at", DateTime(timezone=True), nullable=True),
)

vsrf_act_text = Table(
    "vsrf_act_text",
    metadata,
    Column(
        "pdf_id",
        BigInteger,
        ForeignKey("vsrf_act.pdf_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    ),
    Column("raw_page_id", BigInteger, ForeignKey("raw_page.id"), nullable=True),
    #: Пусто — у PDF нет текстового слоя (скан без распознавания): кандидат
    #: на распознавание потом, а не «акта нет».
    Column("plain_text", Text, nullable=False),
    # tsv — generated-колонка в миграции, как у act_text.
)

vsrf_claim = Table(
    "vsrf_claim",
    metadata,
    Column("claim_id", Text, primary_key=True),
    Column("number", Text, nullable=True),
    Column("received_date", Date, nullable=True),
    Column("case_type", String(32), nullable=False),
    Column("instance", Text, nullable=True),
    Column("case_uid", Text, nullable=True, comment="УИД дела — ключ связи с нашим корпусом"),
    Column("first_court", Text, nullable=True, comment="суд 1-й инстанции, дата решения, судья"),
    Column("first_case_number", Text, nullable=True),
    Column("subject", Text, nullable=True),
    Column("first_seen", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

#: Окно выдачи, пройденное целиком и сверенное со счётчиком сайта.
vsrf_window = Table(
    "vsrf_window",
    metadata,
    Column("source", String(16), primary_key=True, comment="acts | claims"),
    Column("case_type", String(32), primary_key=True),
    Column("window_from", Date, primary_key=True),
    Column("window_to", Date, nullable=False),
    Column("total", Integer, nullable=False),
    Column("done_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


# --- КС РФ (docs/ksrf.md) ---------------------------------------------------

ksrf_decision = Table(
    "ksrf_decision",
    metadata,
    #: Номер в `/doc/KSRFDecision{id}.pdf` — у решения он единственный.
    Column("pdf_id", BigInteger, primary_key=True, autoincrement=False),
    Column("number", Text, nullable=True, comment="напр. 57-П/2026, 2435-О/2026"),
    Column("kind", Text, nullable=True, comment="буква из номера: П, О, Р, З, ПР…"),
    Column("decision_date", Date, nullable=True),
    Column("title", Text, nullable=True),
    Column("first_seen", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("text_fetched_at", DateTime(timezone=True), nullable=True),
)

ksrf_decision_text = Table(
    "ksrf_decision_text",
    metadata,
    Column(
        "pdf_id",
        BigInteger,
        ForeignKey("ksrf_decision.pdf_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    ),
    Column("raw_page_id", BigInteger, ForeignKey("raw_page.id"), nullable=True),
    Column("plain_text", Text, nullable=False),
    # tsv — generated-колонка в миграции, как у act_text.
)
