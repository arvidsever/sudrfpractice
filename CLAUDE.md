# Claude Code: обязательный контекст проекта

Перед любой содержательной работой в этом репозитории прочитай полностью:

1. `AGENTS.md` — общие правила репозитория; они применяются к Claude без
   исключений;
2. `docs/architecture-decisions.md` — архитектурный baseline, зафиксированный
   после сравнительного исследования open-source судебных harvester/search
   систем;
3. `docs/roadmap.md` — текущее состояние, измерения и последовательность работ.

Если задача затрагивает конкретный уже исследованный инвариант, сначала найди и
прочитай соответствующий документ в `docs/`.

## Короткие guardrails на случай узкого контекста

- `sudrfpractice` — server-side harvester/search/API; desktop `Sudrf` — клиент,
  scraping в него не встраивается.
- Immutable raw в S3 важнее производной базы; normalized data должны оставаться
  воспроизводимыми из raw и иметь provenance/parser version.
- Сохраняем небольшой Python/PostgreSQL стек. Не добавляем Selenium/Puppeteer,
  Scrapy, Celery, Redis, Elasticsearch/OpenSearch только потому, что это типичный
  production stack. Сначала нужен измеренный bottleneck.
- Browser automation — только изолированный adapter там, где обычный HTTP
  объективно недостаточен.
- PostgreSQL queue + `FOR UPDATE SKIP LOCKED`/leases/heartbeat предпочтительнее
  Redis/Celery на текущем масштабе; source throttle SUDRF остаётся главным
  ограничителем throughput.
- Current state и append-only semantic change history — разные слои. Не
  подменять domain events generic JSON diff по индексам массивов.
- UID относится к конкретному производству/стадии; связи между инстанциями
  моделируются отдельно, а не схлопывают первичные идентификаторы.
- Search MVP: structured facets + identifiers/trigram + PostgreSQL FTS +
  normalized legal references. Semantic/hybrid — следующий слой, не замена.
- Coverage должна быть query-aware. Текущий `Found.collected_share` в
  `search.py` нельзя выдавать через публичный API как полноту корпуса без
  исправления его семантики.
- Public API — FastAPI, read-only на первом этапе, cursor/keyset pagination.
- Embeddings не должны блокировать MVP/API. Документы и запросы обязаны
  использовать один inference path; перед corpus-wide embedding benchmark
  512/1024, `halfvec`, chunk offsets и retrieval quality; HNSW строить после
  bulk load.
- При конфликте предположения с измерением доверяем измерению и обновляем
  архитектурное решение в `docs/architecture-decisions.md`.

Не копируй архитектуру Juriscraper, CourtListener, CourtFlow или CourtSniffer
целиком: они являются референсами решений и ловушек. Причины выбора и отказа от
их подходов записаны в `docs/architecture-decisions.md`.
