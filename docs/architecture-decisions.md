# Архитектурные решения и ориентиры

Состояние на 22.08.2026. Этот документ фиксирует выводы сравнительного разбора
open-source проектов по сбору и поиску судебной практики и задаёт архитектурный
baseline для `sudrfpractice`. Это не описание уже написанного кода: пункты ниже
надо считать решениями, ограничениями и направлением дальнейшей разработки.

Если конкретный benchmark или фактическое поведение ГАС «Правосудие» противоречит
этому документу, приоритет у измерений. Решение после этого обновляется здесь, а
не обходится локальной заплаткой.

## 1. Граница проекта

`sudrfpractice` — серверная система сбора, нормализации, хранения и поиска
судебной практики. Desktop-приложение `Sudrf` не должно содержать harvester:
оно будет клиентом read-only API этого проекта.

Основная цепочка:

`суды -> HTTP/adapters -> immutable raw -> parser/normalization -> PostgreSQL -> search/API -> Sudrf`

Сырьё в S3 — источник истины и страховка от ошибок парсеров. Нормализованная
PostgreSQL — производное представление, которое можно пересобрать из raw без
повторного многонедельного обхода судов.

## 2. Что показали референсные проекты

| Проект | Что полезно перенять | Что не переносить механически |
| --- | --- | --- |
| [Juriscraper](https://github.com/freelawproject/juriscraper) | строгие адаптеры, разделение scraping и persistence, фикстуры, сбор всей доступной метаинформации | американскую модель судов и browser-first подход |
| [CourtListener](https://github.com/freelawproject/courtlistener) | разделение collection / persistence / async enrichment / search / API | Celery, Redis, Elasticsearch и прочую production-инфраструктуру до появления реальной необходимости |
| [CourtFlow](https://github.com/AlexanderKuzikov/CourtFlow) | адаптер на тип/вариант суда, отдельный справочник судов, stale-only retry, явное понимание UID как идентификатора конкретного производства/стадии | JSON как основное хранилище, внешнюю captcha-службу |
| [CourtSniffer](https://github.com/AlexanderKuzikov/CourtSniffer) | практические особенности SUDRF/msudrf: сессии, captcha, AJAX, court code как стабильный идентификатор | Node/Puppeteer как основной стек |
| [sudrf_parser](https://github.com/alexxmirny/sudrf_parser) | snapshot + change detection как модель мониторинга | diff массивов по позициям вместо семантических идентификаторов |
| [sudrfscraper](https://github.com/tochno-st/sudrfscraper) | широкий охват источников, resume, понимание старых вариантов SUDRF | тяжёлую Java/Selenium-монолитность и ручную captcha |
| [dataout-org/sudrfparser](https://github.com/dataout-org/sudrfparser) | разведку старых форм и необходимость сохранять fingerprint запроса | монолитный parser и 2Captcha/Selenium как архитектурный центр |
| [sudrf-proxy](https://github.com/OlegSirik/sudrf-proxy) | исторически подтверждённую модель «локальное состояние + периодическое сравнение + изменившиеся поля» | старую реализацию и стек |

Главный архитектурный референс — Juriscraper; главный production-референс —
CourtListener; наиболее полезные российские референсы — CourtFlow и CourtSniffer.
Их надо использовать как источники решений и известных ловушек, а не как повод
копировать технологический стек.

## 3. Базовый стек

До появления измеренной причины сохраняем небольшой текущий стек:

- Python 3.12;
- `httpx` для HTTP;
- `selectolax` для HTML;
- Pydantic 2 для моделей/валидации;
- SQLAlchemy Core + psycopg 3;
- Alembic;
- PostgreSQL;
- S3-compatible object storage для immutable raw и backup;
- FastAPI + Uvicorn для read-only API.

Не добавлять по умолчанию Selenium/Puppeteer, Scrapy, Celery, Redis,
Elasticsearch/OpenSearch. Каждый из них допустим только если конкретная задача
не решается проще текущим стеком и это подтверждено измерением.

Browser automation допускается только внутри изолированного adapter-а для
источника, который невозможно устойчиво читать обычным HTTP. Она не должна
становиться общим transport layer.

## 4. Очередь и production runtime

Текущая PostgreSQL-очередь остаётся основной. Следующее развитие очереди —
leases/heartbeat и безопасный захват через `FOR UPDATE SKIP LOCKED`, а не
немедленный переход на Redis/Celery.

На Linux-сервере production-процессы и расписания должны жить в `systemd`
(service/timer). `launchd` остаётся локальным способом поддерживать сбор на Mac,
но не целевой серверной архитектурой.

Глобальный throttle SUDRF важнее числа worker-ов: источник, а не CPU, задаёт
предел harvesting throughput. Масштабирование воркеров не должно обходить общий
лимит платформы.

## 5. Raw, provenance и воспроизводимость

Raw хранится неизменяемо до разбора и архивируется в S3. Для каждого сырого
ответа надо иметь возможность восстановить не только URL, но и достаточный
fingerprint запроса: method, query/form parameters и существенные headers/session
attributes, если без них ответ нельзя воспроизвести или объяснить.

Нормализованные сущности должны нести provenance:

- parser/schema version;
- ссылку на исходный `raw_page`;
- время fetch/parse;
- при необходимости adapter/source version.

Изменение парсера должно позволять понять, какие строки базы получены старой
логикой и требуют reparse.

## 6. Модель дела и инстанций

Не объединять разные судебные инстанции в одно «дело» только потому, что они
логически связаны. UID ГАС — идентификатор конкретного производства/стадии,
а связь первой, апелляционной, кассационной и иных стадий должна быть отдельной
relation-моделью.

Нужны явные связи между производствами, в том числе с уровнем уверенности и
источником связи. Это позволяет не разрушать первичные идентификаторы ради
удобной пользовательской «цепочки дела».

Сохраняется важный tri-state: `act_published = true / false / null`. `false`
означает, что карточка проверена и публикации нет; `null` — карточка ещё не
проверена. Эти состояния нельзя схлопывать.

## 7. Current state и история изменений

Для мониторинга одной текущей карточки недостаточно. Нужны два слоя:

1. current state — последнее нормализованное состояние дела, участников,
   заседаний, жалоб и актов;
2. append-only domain events/change history — фактические изменения между
   наблюдениями.

История должна быть семантической: «назначено заседание», «изменён результат»,
«добавлен акт», «появилась жалоба», а не generic diff JSON-массивов по индексам.
Для повторяющихся сущностей нужен устойчивый либо составной identity key.

Этот слой должен появиться до интеграции push/monitoring в `Sudrf`, иначе клиент
будет вынужден заново вычислять изменения из полных карточек.

## 8. Поиск: сначала надёжный lexical/structured слой

До semantic search основной поиск строится в PostgreSQL:

- структурные facets: суд, картотека, судья, результат, даты, первая инстанция;
- точные/нормализованные идентификаторы;
- `pg_trgm` для tolerant lookup по номерам/именам там, где это оправдано;
- PostgreSQL FTS (`tsvector`, русская конфигурация) по текстам актов;
- отдельная нормализованная таблица ссылок на нормы права;
- уже извлечённые `participant.articles` для уголовных дел и КоАП.

Elasticsearch/OpenSearch вводится только если PostgreSQL после реального
benchmark не даёт нужной релевантности или latency на рабочем корпусе.

API должен использовать cursor/keyset pagination. Deep `OFFSET` не должен быть
публичным контрактом API.

## 9. Полнота выдачи должна быть query-aware

Поиск на неполном корпусе обязан явно сообщать, какая часть релевантного сегмента
собрана. Один глобальный процент для всех запросов вводит пользователя в
заблуждение.

Текущий `Found.collected_share` в `search.py` требует пересмотра: отношение
«число строк, совпавших с запросом / все уже собранные строки» описывает долю
запроса внутри текущей базы, а не полноту сбора.

Coverage следует считать по релевантным `court × cartoteka × time-window` через
`cartoteka_volume` и завершённые harvest windows. Например, запрос по гражданским
делам 2 КСОЮ за 2024 год должен получать собственную оценку покрытия именно этого
сегмента.

Это correctness issue и должно быть исправлено до публичного API.

## 10. Semantic search — не critical path MVP

`Qwen/Qwen3-Embedding-0.6B` остаётся выбранной базовой моделью, но полный
embedding pipeline не должен задерживать первый API.

Перед corpus-wide индексацией надо закрыть четыре вопроса:

1. один production inference path для документов и запросов; MLX и PyTorch
   нельзя смешивать, потому что их пространства уже показали различия в NN;
2. portable runtime для Linux/API либо отдельный embedding service на том же
   inference stack;
3. benchmark retrieval quality на реальных юридических запросах для 512 vs
   1024 dimensions;
4. benchmark хранения: `vector(1024)` vs `halfvec(1024)` и Matryoshka 512.

Для chunk storage предпочтительно сначала проверить хранение offsets в
`act_text`, а не дублирование текста каждого chunk-а в отдельной строке.

HNSW строится после bulk load векторов. Не надо оплачивать постоянное
поддержание большого ANN-индекса во время первоначального наполнения.

Semantic ranking вводится как дополнительный слой hybrid search после
structured + FTS, а не как замена им.

## 11. MVP

Первый полезный серверный продукт должен включать:

- полный listing index всех 10 кассационных судов общей юрисдикции и всех четырёх
  картотек;
- репрезентативный recent card/text corpus: ориентир — последние 12–24 месяца
  по всем судам и картотекам, а historical backfill идёт фоном;
- structured facets;
- поиск по идентификаторам и FTS;
- нормализованный поиск по ссылкам на нормы;
- endpoints карточки дела и текста акта;
- честную query-aware coverage metadata;
- cursor pagination;
- подключение `Sudrf` как клиента API.

Не является блокером MVP: полный исторический card backfill, corpus-wide
embeddings, HNSW, Elasticsearch, Redis/Celery, сложная distributed topology.

## 12. Последовательность разработки

1. Закончить и проверить полный listing corpus и метрики полноты.
2. Укрепить canonical model: stage relations, provenance/parser version,
   current state + domain change events.
3. Собрать recent cards/texts по всем судам и картотекам; historical backfill
   оставить фоновым.
4. Закончить lexical/structured/legal-reference search.
5. Сделать read-only FastAPI API: search, case, act/text, facets, coverage,
   cursor pagination.
6. Подключить `Sudrf` к API.
7. Довести incremental catch-up и domain events до мониторингового режима.
8. Провести retrieval benchmark semantic search: runtime, 512/1024, halfvec,
   chunk representation, hybrid ranking.
9. После bulk embedding построить ANN/HNSW.
10. Только после измеренных bottleneck-ов рассматривать Redis/Celery,
    Elasticsearch/OpenSearch или отдельное vector hardware.

## 13. Правило для новых архитектурных решений

Перед добавлением крупной зависимости или нового инфраструктурного слоя нужно
ответить в PR/документации на три вопроса:

1. Какой измеренный bottleneck или correctness problem он решает?
2. Почему текущий стек этого не решает достаточно хорошо?
3. Какова цена поддержки, миграции и восстановления?

Если ответы основаны только на «так обычно делают», изменение не принимается.
