# ARCHITECTURE-SPINE — Платформа процессинга платежей

> Greenfield-дизайн. Критический маршрут (Significance Score: 13). Все AD — обязательные инварианты для параллельных исполнителей.

---

## AD-1: Saga Orchestration для жизненного цикла платежа

- **Binds**: Payment Orchestrator ↔ все участники платежного потока (Authorization, Fraud, AML, Rail Connectors, Ledger)
- **Prevents**: расхождение «кто отвечает за последовательность шагов платежа» — без оркестратора каждый участник решает сам, поток невидим, компенсации не скоординированы
- **Rule**: жизненный цикл платежа управляется **только** оркестратором (Saga Execution Coordinator). Состояние саги персистентно (таблица `payment_saga`), явная state machine. Компенсации — семантические (сторно, возврат), не удаление. Хореография запрещена для потоков >3 участников.
- **Статус**: [ADOPTED]

## AD-2: Transactional Outbox для надёжной публикации событий

- **Binds**: каждый сервис, пишущий в БД и публикующий события ↔ Kafka
- **Prevents**: dual-write problem — «в БД записано, в Kafka нет» (или наоборот), расхождение контуров
- **Rule**: запись бизнес-изменения и запись события в `outbox` — **одна локальная транзакция**. Ретрансляция — CDC (Debezium, чтение WAL), не polling. Событие — полное самодостаточное тело. Потребитель обязан быть идемпотентным (AD-5). Метрика: размер неотправленного outbox с алертом при росте.
- **Статус**: [ADOPTED]

## AD-3: Database-per-service

- **Binds**: каждый bounded context ↔ собственная схема/БД
- **Prevents**: shared-database coupling — сервисы лезут в чужие таблицы, невозможность независимого деплоя/масштабирования
- **Rule**: один bounded context = одна база данных (PostgreSQL). Cross-service доступ — только через API или события. Запрещён прямой доступ к БД другого сервиса. Исключение: read-replica для аналитики (read-only, отдельный кластер).
- **Статус**: [ADOPTED]

## AD-4: Сильная консистентность для финансового состояния

- **Binds**: Payment Orchestrator, Ledger, Authorization ↔ PostgreSQL (single-writer per payment_id)
- **Prevents**: гонки при параллельной обработке одного платежа, потеря обновлений, двойное списание
- **Rule**: финансовое состояние (статус платежа, остатки, холды) — сильная консистентность. Single-writer per aggregate (payment_id): партиционирование/маршрутизация запросов по payment_id на один обработчик. Оптимистичная блокировка через версию сущности. Уведомления, отчётность, фрод-скоринг — eventual consistency.
- **Статус**: [ADOPTED]

## AD-5: Идемпотентность на каждой точке входа

- **Binds**: Payment Gateway (API), все consumer'ы событий ↔ Redis (idempotency keys) + inbox-таблицы
- **Prevents**: двойное списание при ретраях клиента, дублирующая обработка при at-least-once доставке
- **Rule**: каждый mutating endpoint принимает `Idempotency-Key`. Дедупликация в **той же транзакции**, что и бизнес-эффект (inbox-таблица). Повтор возвращает ПЕРВЫЙ результат. TTL ключей = окно ретраев канала + буфер. Метрика: аномальный рост дублей = алерт.
- **Статус**: [ADOPTED]

## AD-6: Сегментация доверенных зон (4 зоны)

- **Binds**: все компоненты платформы ↔ network segmentation, mTLS, firewall rules
- **Prevents**: проброс атаки из публичного канала к финансовым данным, нарушение PCI DSS / КИИ
- **Rule**: 4 изолированные зоны — (1) DMZ: API Gateway, channel adapters; (2) Application: Orchestrator, Auth, Fraud, AML; (3) Data: Ledger, Clearing; (4) Integration: Rail Connectors. Междузонный трафик — mTLS + network policy. Синхронный вызов через зону — только через service mesh. Пан-данные (номер карты) не покидают Data Zone (токенизация в DMZ).
- **Статус**: [ADOPTED]

## AD-7: Append-only аудит-лог платежа

- **Binds**: Payment Orchestrator ↔ `payment_events` (append-only table)
- **Prevents**: потеря аудиторского следа, невозможность реконструкции жизненного цикла платежа для регулятора/судебного запроса
- **Rule**: каждое изменение состояния платежа — запись в `payment_events` (event_type, payment_id, timestamp, actor, payload, prev_version). Текущий статус материализован из событий. Запрет на UPDATE/DELETE событий. Retention — 5 лет (115-ФЗ). Не путать с event sourcing: current state кэшируется, события — для аудита.
- **Статус**: [ADOPTED]

## AD-8: Circuit breaker на всех внешних вызовах

- **Binds**: Rail Connectors ↔ внешние системы (NSPK, CBR, SWIFT, Core Banking)
- **Prevents**: каскадный отказ при недоступности внешней системы, истощение connection pool
- **Rule**: каждый вызов внешней системы — через circuit breaker (closed/open/half-open). Таймаут на вызов: card < 10s, SBP < 3s, SWIFT < 30s, CBR < 30s. При открытом breaker — быстрый fail с явным статусом (RAIL_UNAVAILABLE), не таймаут. Метрика: состояние breaker'ов дашборд.
- **Статус**: [ADOPTED]

---

## Deferred (с условиями возврата)

### DEF-1: Event Sourcing (полный)
- **Причина**: AD-7 (append-only аудит-лог) даёт 80% ценности при 20% сложности. Полный ES требует snapshotting, projection rebuilding, versioning — избыточно для MVP.
- **Условие возврата**: если потребуется replay платёжного потока в тестовом контуре или миграция схемы событий без даунтайма.

### DEF-2: Multi-region active-active
- **Причина**: single-region active-passive с RPO=0 (синхронная репликация) достаточно для SLA 99.99%. Active-active требует конфликт-резолюции для финансового состояния.
- **Условие возврата**: если RTO < 5 мин или требование георезервирования от надзора.

### DEF-3: Stream processing для fraud (Flink)
- **Причина**: rules-based + ML-batch scoring достаточно для MVP. Real-time stream processing — следующий горизонт.
- **Условие возврата**: если latency fraud-проверки > 100ms при росте нагрузки или переход к behavioral biometrics.

---

## AD-9: Dead Letter Queue + стратегия ядовитых сообщений

- **Binds**: все Kafka consumers ↔ DLQ-топики `*.dlq`, операционная очередь ручной обработки
- **Prevents**: poison message блокирует партицию → стоп обработки платежей; невалидное сообщение повторяется бесконечно
- **Rule**: каждый consumer имеет политику retry: 3 попытки с экспоненциальной задержкой (1s, 5s, 25s), затем — запись в DLQ-топик с полным контекстом (message, error, stack trace, original offset). DLQ-топики мониторятся: алерт при росте > 0. Сообщения из DLQ обрабатываются оператором через отдельный инструмент (replay/reject/fix). Запрещён бесконечный retry. TTL сообщений в retry-топиках: 24ч.
- **Статус**: [ADOPTED]

## AD-10: Стратегия ретраев (backoff + jitter, один слой)

- **Binds**: все межсервисные и внешние вызовы ↔ circuit breaker + retry policy
- **Prevents**: «ретраи добили зависимость» — каскадный отказ при перегрузке; ретраи на каждом уровне стека → экспоненциальный рост нагрузки
- **Rule**: ретраи — **только в одном слое** стека (обычно — Rail Connector или Orchestrator). Экспоненциальный backoff с джиттером: `delay = base * 2^attempt + random(0, base)`. Token bucket для ограничения concurrent ретраев. Ретраи — только для транзиентных сбоев (timeout, 5xx, connection refused); 4xx — не ретраить. Таймаут вызова выбирается по p99.9 latency зависимости, не «по умолчанию». Запрещены ретраи на каждом уровне (Gateway → Orchestrator → Connector — ретрай только на одном).
- **Статус**: [ADOPTED]

## AD-11: Сверка (reconciliation) с рельсами

- **Binds**: Clearing & Settlement ↔ Rail Connectors (NSPK, CBR, SWIFT), Ledger
- **Prevents**: расхождение между внутренним состоянием платформы и внешним рельсом — не выявленные финансовые расхождения
- **Rule**: end-of-day сверка: получение реестров от каждого рельса, auto-match по payment_id/amount/status, unmatched записи → exception queue (ручная разборка). Сверка идёт в обоих направлениях: платформа → рельс (отправленные, но не подтверждённые) и рельс → платформа (полученные от рельса, но не в платформе). Результат сверки: `reconciliation_report(date, rail, matched, unmatched_platform, unmatched_rail, exceptions)`. Алерт при unmatched > 0. Сверка — предусловие для settlement.
- **Статус**: [ADOPTED]

## AD-12: Disaster Recovery (active-passive, RTO=15min, RPO=0)

- **Binds**: все компоненты платформы ↔ DR-сайт, sync replication, failover runbook
- **Prevents**: потеря данных при отказе основного ЦОД; недоступность процессинга > 15 мин
- **Rule**: active-passive: основной ЦОД — active, DR-сайт — hot standby. PostgreSQL: синхронная репликация (synchronous_commit=on, synchronous_standby_names) → RPO=0. Kafka: MirrorMaker2 в режим sync (acks=all, min.insync.replicas=2). Redis: replica в DR-сайте. Failover: автоматическое для stateless-сервисов (K8s), ручное для stateful (PostgreSQL promote). DR drill: плановое переключение раз в квартал (проверка RTO). Runbook: пошаговая процедура failback. Метрика: `dr_drill_rto_actual` — фактическое время восстановления.
- **Статус**: [ADOPTED]

## AD-13: Secrets management (Vault + HSM)

- **Binds**: все сервисы ↔ HashiCorp Vault, HSM
- **Prevents**: секреты в коде/configmaps/env vars — компрометация, отсутствие ротации, нарушение PCI DSS
- **Rule**: все секреты (БД-credentials, API-ключи рельсов, mTLS-ключи) — в HashiCorp Vault. Динамические credentials для PostgreSQL (Vault Database Engine): short-lived, auto-rotation. HSM — для криптографических ключей (PIN block encryption, card data crypto): ключи не покидают HSM, операции — через PKCS#11. Ротация: mTLS-сертификаты — 90 дней (cert-manager), БД-credentials — 1 час (dynamic), HSM keys — по графику PCI DSS. Запрещено хранение секретов в Kubernetes Secrets (кроме временных, для bootstrap Vault). Audit: Vault audit log — кто, когда, какой секрет запросил.
- **Статус**: [ADOPTED]

## AD-14: API versioning + backward compatibility

- **Binds**: Payment Gateway API ↔ все каналы (мобильный банк, Open API, партнёры)
- **Prevents**: breaking change ломает потребителей без предупреждения; невозможность параллельного использования версий
- **Rule**: семантическое версионирование API: `major.minor`. Версия — в HTTP-заголовке `Accept: application/vnd.bank.payments.v2+json` (header-based, не URL-path). Backward-compatible изменения (additive: новые поля, новые endpoints) — minor bump, не ломают потребителей. Breaking changes (удаление поля, изменение типа) — major bump, старая версия поддерживается 12 месяцев с deprecation notice (header `Sunset`). CI-гейт: schemathesis-проверка совместимости схемы. OpenAPI-спецификация — источник истины, автогенерация клиентов.
- **Статус**: [ADOPTED]

## AD-15: Schema Registry для Kafka

- **Binds**: все продюсеры и консьюмеры событий ↔ Confluent Schema Registry
- **Prevents**: несовместимые изменения схемы события → consumer падает или теряет данные; отсутствие контракта событий
- **Rule**: формат событий — Avro (compact, schema-evolvable). Confluent Schema Registry — единый реестр схем. Compatibility: BACKWARD по умолчанию (новый consumer читает старые сообщения). Breaking change схемы — новая версия + стратегия миграции (dual-write period). CI-гейт: регистрация схемы в Schema Registry → проверка совместимости → при несовместимости — fail build. Запрещён JSON без схемы для событий платежа.
- **Статус**: [ADOPTED]

## AD-16: Стратегия развёртывания (canary + zero-downtime DB migrations)

- **Binds**: все сервисы ↔ Kubernetes deployment, CI/CD pipeline
- **Prevents**: downtime при деплое; миграция БД блокирует таблицы; битый релиз уходит на весь трафик
- **Rule**: canary deployment: 5% → 25% → 50% → 100% трафика, автоматический rollback при росте error rate > threshold (5 мин окно). Blue-green для Orchestrator и Ledger (stateful, критичные). Zero-downtime DB migrations: expand-contract pattern — (1) expand: добавить колонку/таблицу (обратно-совместимо), (2) migrate: заполнить данные, dual-write, (3) contract: удалить старое. Запрещены миграции с блокировкой таблиц на продакшене (ALTER TABLE lock). Миграции — через Flyway, каждая в транзакции, reversible.
- **Статус**: [ADOPTED]

## AD-17: Queue Load Leveling для пиковых нагрузок

- **Binds**: Payment Gateway ↔ Payment Orchestrator (через Kafka command topic)
- **Prevents**: зарплатные дни, валютные сессии — пиковая нагрузка 5–10× от средней → отказ процессинга при прямой синхронной обработке
- **Rule**: Payment Gateway публикует команду `ProcessPayment` в Kafka-топик `payments.commands` (не синхронный вызов Orchestrator). Orchestrator — competing consumers (N instances), масштабирование по lag. Backpressure: при lag > threshold → Gateway возвращает 202 Accepted (принято, будет обработано), не 200. Пиковые периоды: предмасштабирование по расписанию (зарплатные дни 25–27 числа). Приоритизация: отдельные партиции/топики для high-priority платежей (СБП, карты) vs low-priority (батч-переводы).
- **Статус**: [ADOPTED]

## AD-18: Fee/Commission engine

- **Binds**: Payment Orchestrator ↔ Fee Engine (новый bounded context), Ledger
- **Prevents**: расчёт комиссии захардкожен в оркестраторе — невозможность тарифных планов, изменение требует деплоя
- **Rule**: отдельный bounded context Fee Engine: тарифные планы (per channel, per rail, per partner), многоуровневые правила (процент + фикс + tiered), распределение (банк/НСПК/партнёр). Расчёт — синхронный вызов из Orchestrator (после авторизации, до проводки). Результат — запись в Ledger отдельной проводкой (комиссия = доход банка). Тарифы — конфигурируются без деплоя (DB-driven, admin UI). Версионирование тарифов: какая версия применена к платежу — фиксируется в payment_events.
- **Статус**: [ADOPTED]

## AD-19: Dispute/Chargeback flow

- **Binds**: Rail Connectors (card) ↔ Dispute Service (новый bounded context), Orchestrator
- **Prevents**: chargeback обрабатывается вручную — потеря сроков, штрафы от платёжной системы
- **Rule**: отдельный bounded context Dispute Service: приём ISO 8583 chargeback messages (message type 1420/1430), отдельная сага (оркестрация: получить → валидировать → проверить транзакцию → принять решение → проводка → ответ). Статусы: `DISPUTE_OPEN → DISPUTE_REVIEWED → DISPUTE_ACCEPTED/REJECTED → DISPUTE_SETTLED`. Сроки: Visa/MC — 45 дней на ответ, NSPK — 30 дней. Алерт при приближении дедлайна. Двойная запись в Ledger: возврат суммы + комиссия за chargeback (если применимо). Связь с оригинальным платежом по payment_id.
- **Статус**: [ADOPTED]

## AD-20: Вебхук/колбэк-стратегия для async рельсов

- **Binds**: Rail Connectors (SWIFT, БЭСП) ↔ Orchestrator, callback endpoint
- **Prevents**: async-ответ от рельса не коррелируется с платежом → платёж «зависает» в RAIL_SENT
- **Rule**: async-рельсы (SWIFT, БЭСП) — двунаправленная корреляция: (1) исходящий: payment_id в поле_reference (назначение платежа / remittance info), (2) входящий: callback endpoint `/callbacks/{rail}` принимает ответ, извлекает payment_id из reference. Таймаут ожидания ответа: SWIFT — 24ч, БЭСП — 1ч. При таймауте: переход в `RAIL_TIMEOUT` → компенсация (если возможно) или эскалация в операционную очередь. Polling fallback:.periodic status query (MT199 для SWIFT). Callback endpoint — идемпотентный (AD-5), mTLS, IP-allowlist.
- **Статус**: [ADOPTED]

## AD-21: Data lifecycle — 152-ФЗ vs append-only audit

- **Binds**: все сервисы ↔ data retention, pseudonymization policy
- **Prevents**: конфликт между правом на забвение (152-ФЗ) и требованием сохранения фин. данных (115-ФЗ, 5 лет)
- **Rule**: разделение данных по классам: (1) Финансовые транзакции (payment, payment_events, ledger) — retention 5 лет (115-ФЗ), удаление запрещено. Право на забвение НЕ применяется. (2) Персональные данные клиента (ФИО, телефон, email в metadata) — псевдонимизация по запросу: замена на token, сохранение связи через отдельный secure mapping. (3) Технические данные (логи, traces) — retention 90 дней, затем удаление. (4) Карточные данные (PAN, CVV) — не хранятся (токенизация в DMZ, AD-6). Data minimization: в событиях Kafka — минимум ПДн, только payment_id и статус.
- **Статус**: [ADOPTED]

## AD-22: Rate limiting per channel/partner

- **Binds**: API Gateway ↔ все каналы (мобильный банк, Open API, партнёры)
- **Prevents**: один партнёр/канал заддосил процессинг → отказ для всех; отсутствует честное распределение ресурсов
- **Rule**: token bucket per tenant (channel + client_id): разные лимиты для разных каналов (банковский канал: 10000 RPS, партнёр: 100 RPS, Open API: 500 RPS). При превышении — 429 Too Many Requests + заголовок `Retry-After`. Приоритизация: банковский канал > партнёры (очередь/权重). Burst: 2× steady-state на 10 секунд. Алерт при стабильно высоком 429-rate для канала (возможно нужнаcapacity-ревизия). Конфигурация лимитов — без деплоя (DB-driven).
- **Статус**: [ADOPTED]

## AD-23: Контрактное тестирование

- **Binds**: все пары consumer↔provider ↔ CI/CD pipeline
- **Prevents**: изменение контракта одного сервиса ломает consumer молча (обнаруживается в продакшене)
- **Rule**: consumer-driven contract testing (Pact): каждый consumer описывает ожидания от provider. CI-гейт: изменение provider → прogoн Pact-тестов → при несовпадении — fail build. Schemathesis для OpenAPI-спецификаций: авто-генерация тест-кейсов по схеме. Контракты Kafka-событий — через Schema Registry compatibility checks (AD-15). Отдельный pipeline stage: `contract-tests` — обязателен для merge в main.
- **Статус**: [ADOPTED]

## AD-24: Feature flags

- **Binds**: все сервисы ↔ feature flag service (например, Unleash/LaunchDarkly)
- **Prevents**: новый рельс/фича требует деплоя для включения; нет kill switch для экстренного отключения канала
- **Rule**: feature flags для: новых рельсов (включение по банкам), A/B-тестов, canary-фич, kill switch (экстренное отключение канала/партнёра). Типы флагов: on/off, percentage rollout, targeted (по bank_id/channel). Конфигурация — без деплоя (feature flag service). Kill switch — отдельный флаг с policy: включение только через approval (двое рук). Audit: кто, когда, какой флаг изменил. Метрика: `feature_flag_evaluations_total` — мониторинг использования.
- **Статус**: [ADOPTED]

## AD-25: Chaos engineering

- **Binds**: все компоненты ↔ chaos testing platform, game day schedule
- **Prevents**: отказоустойчивость не валидирована на практике; скрытые каскадные зависимости выявляются только в инциденте
- **Rule**: плановые game days (раз в квартал): (1) kill pod случайного сервиса, (2) network partition между зонами, (3) PostgreSQL failover, (4) Kafka broker down, (5) Redis unavailable, (6) Rail Connector timeout. Метрики: время обнаружения, время восстановления, affected transactions. Chaos — только в staging (или canary segment продакшена с 1% трафика). Результаты — в postmortem и обновление runbooks. Запрещён chaos в peak hours.
- **Статус**: [ADOPTED]

## AD-26: Operator actions audit

- **Binds**: все операционные действия (ручной ввод, отмена, форсирование статуса) ↔ operator audit log
- **Prevents**: действия оператора невидимы — невозможность разбора инцидента, нарушение разделения полномочий
- **Rule**: каждое действие оператора (manual override, force status, manual reconciliation, DLQ replay) — запись в `operator_audit(operator_id, action, target_payment_id, reason, before_state, after_state, timestamp, ip).` Отдельно от `payment_events` (системные события). Требует reason (обоснование) — обязательное поле. RBAC: разные роли — разные действия (operator, supervisor, admin). Dual control: критичные действия (force COMPLETED, manual settlement) — требуют двух подписей. Alert: аномальное количество manual overrides оператором.
- **Статус**: [ADOPTED]

## AD-27: Multi-currency / FX

- **Binds**: Payment Orchestrator ↔ FX Service (новый bounded context), Ledger
- **Prevents**: конвертация валют захардкожена — невозможность мультивалютных платежей (SWIFT)
- **Rule**: отдельный bounded context FX Service: курсы валют (ежедневные от treasury, +real-time для major pairs), spread (конфигурируемый per channel/rail). Конвертация: `amount_from * rate = amount_to`, фиксация курса в момент авторизации (rate lock). Запись в Ledger: двойная проводка в двух валютах + проводка курсовой разницы (доход банка). Курсы — версионные: какая версия курса применена — в payment_events. Округление: банковское (half-up), до 2 знаков (или 4 для экзотических валют). Запрещён плавающий курс после авторизации.
- **Статус**: [ADOPTED]
