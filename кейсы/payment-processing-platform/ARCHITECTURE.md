# ARCHITECTURE.md — Платформа процессинга платежей

> Greenfield, Critical-маршрут (Significance Score: 13). Документ — целевая архитектура для handoff кодовым командам.

---

## 1. Бизнес-контекст

Платформа процессинга платежей банка — центральный узел, проводящий платежи через множество рельсов:

| Рельс | Тип | Latency-бюджет | Регулятор |
|-------|-----|----------------|-----------|
| Карты (MIR/Visa/MC) | Acquiring + Issuing | p99 < 2s | PCI DSS, NSPK |
| СБП (Cистема быстрых платежей) | C2C, C2B, B2C | p99 < 500ms | НСПК, 161-ФЗ |
| SWIFT | Cross-border | p99 < 30s | SWIFT GPI, 115-ФЗ |
| БЭСП (ЦБ РФ) | Large-value | p99 < 30s | ЦБ РФ |
| Внутренние переводы | Intra-bank | p99 < 1s | — |

**Каналы**: мобильный банк, веб-банк/ДБО, Open API (партнёры/маркетплейсы), ATM/POS.

---

## 2. Bounded Contexts (DDD)

| Контекст | Зона | Ответственность | БД |
|----------|------|----------------|-----|
| **Payment Gateway** | DMZ→App | Приём запросов, идемпотентность, валидация схемы | PG (pgw_db) |
| **Payment Orchestrator** | App | Saga-координация, state machine, маршрутизация на рельс | PG (orc_db) |
| **Authorization** | App | Лимиты, холды, проверка баланса, взаимодействие с ядром | PG (auth_db) + Redis |
| **Fraud Screening** | App | Правила, скоринг, блокировки | PG (fraud_db) + Redis |
| **AML / Sanctions** | App | Санкционный скрининг, ПФЛ/ФОТ, отчётность 115-ФЗ | PG (aml_db) |
| **Rail Connectors** | Int | Адаптеры к NSPK, CBR, SWIFT, Core Banking | PG (per-rail) |
| **Ledger** | Data | Двойная запись, проводки, гласный журнал | PG (ledger_db) |
| **Clearing & Settlement** | Data | Батч-клиринг, сверка, settlement files | PG (clearing_db) |
| **Notification** | App | SMS, push, email — идемпотентный consumer | PG (notif_db) |
| **Reporting** | Data | Отчёты ЦБ, операционные дашборды | PG read-replica |
| **Reconciliation** | Data | End-of-day сверка с рельсами, exception queue | PG (recon_db) |
| **Fee Engine** | App | Тарифные планы, расчёт комиссий, распределение | PG (fee_db) |
| **Dispute Service** | App | Chargeback flow (ISO 8583), арбитраж, сроки | PG (dispute_db) |
| **FX Service** | App | Курсы валют, конвертация, rate lock, spread | PG (fx_db) + Redis |
| **Feature Flags** | Infra | Конфигурация фич, canary, kill switch | Unleash (PG) |
| **Secrets (Vault)** | Infra | Dynamic credentials, key rotation, audit | Vault + HSM |

---

## 3. Архитектурные решения (краткая сводка)

| AD | Решение | ADR |
|----|---------|-----|
| AD-1 | Saga Orchestration (не хореография) | ADR-001 |
| AD-2 | Transactional Outbox + Debezium CDC | ADR-002 |
| AD-3 | Database-per-service (PostgreSQL) | ADR-003 |
| AD-4 | Сильная консистентность для фин. состояния (single-writer per payment_id) | AD-4 (spine) |
| AD-5 | 3-уровневая идемпотентность (Redis + inbox + state machine) | ADR-005 |
| AD-6 | 4 доверенные зоны (DMZ / App / Data / Integration) | ADR-004 |
| AD-7 | Append-only аудит-лог платежа (payment_events) | AD-7 (spine) |
| AD-8 | Circuit breaker на всех внешних вызовах | AD-8 (spine) |
| AD-9 | DLQ + стратегия ядовитых сообщений | ADR-007 |
| AD-10 | Ретраи: backoff+jitter, один слой | ADR-008 |
| AD-11 | Сверка (reconciliation) с рельсами | ADR-009 |
| AD-12 | Disaster Recovery (active-passive, RTO=15min, RPO=0) | ADR-010 |
| AD-13 | Secrets management (Vault + HSM) | ADR-011 |
| AD-14 | API versioning + backward compatibility | ADR-012 |
| AD-15 | Schema Registry для Kafka (Avro) | AD-15 (spine) |
| AD-16 | Canary/blue-green + zero-downtime DB migrations | ADR-013 |
| AD-17 | Queue Load Leveling для пиков | AD-17 (spine) |
| AD-18 | Fee/Commission engine | AD-18 (spine) |
| AD-19 | Dispute/Chargeback flow | AD-19 (spine) |
| AD-20 | Вебхук/колбэк для async рельсов | ADR-014 |
| AD-21 | Data lifecycle: 152-ФЗ vs append-only | ADR-015 |
| AD-22 | Rate limiting per channel/partner | AD-22 (spine) |
| AD-23 | Контрактное тестирование (Pact) | AD-23 (spine) |
| AD-24 | Feature flags | AD-24 (spine) |
| AD-25 | Chaos engineering (game days) | ADR-016 |
| AD-26 | Operator actions audit | AD-26 (spine) |
| AD-27 | Multi-currency / FX | AD-27 (spine) |

---

## 4. NFR-цели (измеримые)

| NFR | Цель | Метод измерения |
|-----|------|----------------|
| Latency (card auth) | p99 < 2s, p99.9 < 5s | Histogram: `payment_auth_duration_seconds` |
| Latency (SBP) | p99 < 500ms | Histogram: `sbp_payment_duration_seconds` |
| Throughput | 5000 TPS peak | Counter: `payments_processed_total` |
| Availability (initiation) | 99.99% (~52 min/year) | Uptime probe на API Gateway |
| Availability (processing) | 99.95% (~4.4h/year) | Success rate: completed/total |
| RTO | 15 min | DR drill (planned) |
| RPO | 0 (no data loss) | Sync replication verification |
| Fraud detection latency | p99 < 100ms | Histogram: `fraud_check_duration_seconds` |
| Outbox lag | < 100ms p99 | Gauge: `outbox_unpublished_count` |
| Data retention | 5 лет (115-ФЗ) | Partition age audit |
| Reconciliation completeness | 100% unmatched → exception queue | Daily reconciliation report |
| DR drill RTO | < 15 min (validated quarterly) | DR drill metrics |
| DR drill RPO | 0 (no data loss) | Sync replication verification |
| DLQ processing time | < 4h (manual review) | DLQ message age gauge |
| Schema compatibility | 100% backward compatible | CI gate: schema-registry check |
| Error budget | 99.99% → 52 min/month budget | SLO burn rate alert |

---

## 5. Payment Flow (Saga)

### Нормальный путь (успешный платёж)

```
Channel → API Gateway → Payment Gateway
  → Orchestrator: INITIATED
    → Fraud Screening (sync, <100ms)
    → AML/Sanctions (sync, <200ms)
    → Authorization: limits + hold (sync, <500ms)
    → Rail Connector: send (async)
    → Rail callback: RAIL_CONFIRMED
    → Ledger: double-entry post (sync)
    → Outbox: publish PaymentCompleted
    → COMPLETED
  ← 200 {payment_id, status: COMPLETED}
```

### Путь компенсации (сбой после RAIL_SENT)

```
RAIL_SENT → Rail timeout/failure
  → Compensating saga:
    C1: Rail Connector — cancel/reverse (если возможно)
    C2: Authorization — release hold
    C3: Ledger — storno posting
  → COMPENSATED
  ← 200 {payment_id, status: COMPENSATED, reason}
```

### Точка невозврата

- **RAIL_SENT** — после отправки на рельс отмена невозможна, только компенсация (возврат).
- До RAIL_SENT — отмена (CANCELLED).

---

## 6. Data Model (ключевые таблицы)

### Payment Orchestrator

```sql
-- Текущее состояние платежа (single-writer per payment_id)
CREATE TABLE payment (
    payment_id      UUID PRIMARY KEY,
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    channel         VARCHAR(32) NOT NULL,
    rail            VARCHAR(32) NOT NULL,  -- card, sbp, swift, besp, internal
    status          VARCHAR(32) NOT NULL,  -- state machine
    amount          NUMERIC(18,2) NOT NULL,
    currency        CHAR(3) NOT NULL,
    payer_account   VARCHAR(32) NOT NULL,
    payee_account   VARCHAR(32),
    metadata        JSONB,
    version         INTEGER NOT NULL DEFAULT 1,  -- optimistic locking
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only аудит-лог (AD-7)
CREATE TABLE payment_events (
    event_id    BIGSERIAL PRIMARY KEY,
    payment_id  UUID NOT NULL REFERENCES payment(payment_id),
    event_type  VARCHAR(64) NOT NULL,
    prev_status VARCHAR(32),
    new_status  VARCHAR(32) NOT NULL,
    actor       VARCHAR(128),  -- system, user_id, operator_id
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (created_at);

-- Outbox (AD-2)
CREATE TABLE outbox (
    id          BIGSERIAL PRIMARY KEY,
    aggregate_id UUID NOT NULL,
    event_type  VARCHAR(64) NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);
```

### Authorization

```sql
-- Лимиты (кэшируются в Redis)
CREATE TABLE account_limits (
    account_id      VARCHAR(32) PRIMARY KEY,
    daily_limit     NUMERIC(18,2) NOT NULL,
    monthly_limit   NUMERIC(18,2) NOT NULL,
    daily_used      NUMERIC(18,2) NOT NULL DEFAULT 0,
    monthly_used    NUMERIC(18,2) NOT NULL DEFAULT 0,
    version         INTEGER NOT NULL DEFAULT 1
);

-- Холды
CREATE TABLE holds (
    hold_id     UUID PRIMARY KEY,
    payment_id  UUID NOT NULL,
    account_id  VARCHAR(32) NOT NULL,
    amount      NUMERIC(18,2) NOT NULL,
    status      VARCHAR(16) NOT NULL,  -- ACTIVE, RELEASED, CONFIRMED
    expires_at  TIMESTAMPTZ NOT NULL
);
```

### Idempotency (Redis + PG inbox)

```sql
-- Inbox для consumer'ов (AD-5, уровень 2)
CREATE TABLE processed_messages (
    message_id  VARCHAR(128) PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    result      JSONB
);
```

---

## 7. Deployment Topology

```
┌─────────────────────────────────────────────────────────┐
│                    Kubernetes Cluster                    │
│                                                          │
│  ┌─────────┐  ┌──────────────────────────────────────┐  │
│  │ DMZ NS  │  │ Application Namespace                 │  │
│  │         │  │  ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐    │  │
│  │ Gateway │──│  │ PGW │ │ ORC │ │AUTH │ │FRD  │    │  │
│  │ Tokeniz │  │  └─────┘ └─────┘ └─────┘ └─────┘    │  │
│  └─────────┘  │  ┌─────┐ ┌─────┐                     │  │
│       │       │  │ AML │ │ NTF │                     │  │
│       │       │  └─────┘ └─────┘                     │  │
│       │       └──────────────────────────────────────┘  │
│       │       ┌──────────────────────────────────────┐  │
│       │       │ Data Namespace                       │  │
│       ├──────│  ┌─────┐ ┌─────┐ ┌─────┐             │  │
│       │       │  │ LED │ │ CLR │ │ EVT │             │  │
│       │       │  └─────┘ └─────┘ └─────┘             │  │
│       │       └──────────────────────────────────────┘  │
│       │       ┌──────────────────────────────────────┐  │
│       │       │ Integration Namespace               │  │
│       ├──────│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐│  │
│               │  │NSPK  │ │ CBR  │ │SWIFT │ │ CORE ││  │
│               │  └──────┘ └──────┘ └──────┘ └──────┘│  │
│               └──────────────────────────────────────┘  │
│                                                          │
│  Service Mesh: Istio (mTLS, traffic management)          │
│  Debezium: CDC connectors per PostgreSQL instance        │
│  Kafka: 3-broker cluster, RF=3, min.insync.replicas=2   │
└─────────────────────────────────────────────────────────┘
         │                              │
    ┌────┴────┐               ┌─────────┴────────┐
    │PostgreSQL│               │ External Systems │
    │ HA pair  │               │ NSPK, CBR, SWIFT │
    │ sync repl│               │ Core Banking     │
    └─────────┘               └──────────────────┘
```

### Ресурсы (оценка для MVP)

| Компонент | Реплики | CPU | RAM | Storage |
|-----------|---------|-----|-----|---------|
| API Gateway | 3 | 2 | 4GB | — |
| Payment Gateway | 3 | 2 | 4GB | — |
| Orchestrator | 4 | 4 | 8GB | — |
| Authorization | 3 | 2 | 4GB | — |
| Fraud | 2 | 4 | 8GB | — |
| AML | 2 | 2 | 4GB | — |
| Rail Connectors | 2 per rail | 2 | 4GB | — |
| Ledger | 3 | 4 | 8GB | — |
| Kafka brokers | 3 | 4 | 16GB | 1TB each |
| PostgreSQL (per service) | 1+1 (HA) | 8 | 32GB | 500GB each |

---

## 8. Observability

### Метрики (Prometheus)
- `payment_auth_duration_seconds` (histogram, labels: rail, channel)
- `payments_processed_total` (counter, labels: rail, status)
- `outbox_unpublished_count` (gauge) — алерт при > 100
- `idempotency_duplicates_total` (counter) — алерт при аномальном росте
- `circuit_breaker_state` (gauge, labels: rail) — алерт при open > 5min
- `saga_stuck_count` (gauge) — алерт при > 0 (застрявшие саги)
- `dlq_messages_total` (counter) + `dlq_queue_depth` (gauge) — алерт при > 0
- `reconciliation_unmatched_count` (gauge) — алерт при > 0 после auto-match
- `circuit_breaker_retry_count` (counter, labels: rail) — алерт при росте
- `rate_limit_429_total` (counter, labels: channel) — алерт при стабильно высоком rate
- `dr_drill_rto_actual` (gauge) — фактическое время DR drill
- `error_budget_remaining_minutes` (gauge) — алерт при < 20% остатка
- `feature_flag_changes_total` (counter) — мониторинг изменений
- `operator_audit_actions_total` (counter, labels: action_type) — алерт при аномалиях
- `vault_secret_requests_total` (counter) — мониторинг обращений

### Трассировка (OpenTelemetry)
- Сквозной trace от API Gateway до Rail Connector.
- Span per saga step.
- Trace propagation через Kafka headers.

### Логирование (ELK)
- Structured logging (JSON).
- Запрещено логирование PAN, CVV, полных номеров счетов (PCI DSS).
- Корреляционный ID (payment_id) во всех логах.

### Алерты
- P1: outbox lag > 1000, saga stuck > 0, availability < 99.9% rolling 1h, DLQ depth > 0, error budget < 10%
- P2: latency p99 > target, circuit breaker open, duplicate rate > 5%, reconciliation unmatched > 0, 429 rate > 10% per channel
- P3: disk usage > 80%, replica lag > 10s, feature flag change, Vault request latency > 500ms

### SLO / SLI / Error Budget

| SLI | Target | Error Budget | Window |
|-----|--------|-------------|--------|
| Availability (payment initiation) | 99.99% | 4.3 min/month | 30 days |
| Availability (payment processing) | 99.95% | 21.6 min/month | 30 days |
| Latency (card, p99) | < 2s | 1% budget on p99.9 violations | 30 days |
| Latency (SBP, p99) | < 500ms | 1% budget on p99.9 violations | 30 days |
| Data integrity (reconciliation) | 100% matched | 0 unmatched allowed (exception queue) | Daily |

**Error budget policy**: при истощении бюджета (< 20% остатка) — freeze feature-деплоев, только hotfix и resilience-улучшения. Восстановление бюджета → разблокировка.

---

## 9. Security & Compliance

| Требование | Реализация | AD/ADR |
|------------|------------|--------|
| PCI DSS | Токенизация PAN в DMZ, HSM для криптоопераций, no-PAN-logging | AD-6, AD-13, ADR-011 |
| 152-ФЗ (ПДн) | Псевдонимизация (не удаление), маскирование, data minimization | AD-21, ADR-015 |
| 115-ФЗ (ПОД/ФТ) | AML-скрининг в реальном времени, retention 5 лет, отчётность | AD-7, AD-21 |
| 187-ФЗ (КИИ) | Сегментация зон, DR-контур (RTO=15min, RPO=0), регистрация КИИ | AD-6, AD-12, ADR-010 |
| 161-ФЗ (НСС) | Соответствие требованиям к национальной платёжной системе | — |
| Secrets management | Vault (dynamic DB creds), HSM (crypto keys), auto-rotation | AD-13, ADR-011 |
| Operator audit | RBAC + dual control + audit trail для ручных действий | AD-26 |

---

## 10. Walking Skeleton (первый сквозной срез)

**Цель**: доказать архитектуру одним сквозным сценарием до массовой разработки.

**Сценарий**: внутренний перевод (intra-bank) от счёта A к счёту B.

```
POST /payments
  Idempotency-Key: test-001
  { "rail": "internal", "amount": 100.00, "currency": "RUB",
    "payer_account": "A", "payee_account": "B" }
```

**Что должно работать**:
1. API Gateway принимает запрос, mTLS, rate limit.
2. Payment Gateway: идемотентность (Redis), валидация, создание payment_id.
3. Orchestrator: saga (state machine), INTERNAL rail (без внешних вызовов).
4. Authorization: проверка баланса (mock Core Banking), холд.
5. Ledger: двойная проводка (Debit A / Credit B).
6. Outbox: событие PaymentCompleted → Kafka.
7. Notification: идемпотентный consumer (mock SMS).
8. Повторный POST с тем же Idempotency-Key → тот же результат.

**Критерий готовности skeleton**: повторный запрос возвращает тот же payment_id, баланс изменился один раз, событие в Kafka получено один раз, audit log содержит все переходы статусов.

---

## 11. Дорожная карта (горизонты)

| Горизонт | Содержание | Длительность |
|----------|-----------|--------------|
| **H0: Walking Skeleton** | Внутренний перевод, сквозной срез, доказательство архитектуры | 4–6 нед |
| **H1: Карты (MIR)** | Acquiring, ISO 8583, HSM, клиринг | 3–4 мес |
| **H2: СБП** | C2C/C2B, NSPK коннектор, реалтайм | 2–3 мес |
| **H3: SWIFT + БЭСП** | Cross-border, large-value, batch | 3–4 мес |
| **H4: Production hardening** | DR, load testing, chaos, observability, fraud ML | 2–3 мес |

---

## 12. Риски (открытые)

| Риск | Вероятность | Влияние | Митигация |
|------|------------|---------|-----------|
| Debezium CDC — операционная сложность | Средняя | Среднее | Polling publisher как fallback, метрика outbox_lag |
| Core Banking API — недокументированное | Высокая | Высокое | Обследование ядра до H1, ACL-адаптер, mock для skeleton |
| HSM-интеграция — длинный цикл закупки/настройки | Высокая | Высокое | Начать закупку параллельно с H0 |
| 5000 TPS — неvalidated | Средняя | Среднее | Load testing на H0, горизонтальное масштабирование |
| Fraud ML — нет данных для обучения | Высокая | Среднее | Rules-based на MVP, сбор данных с H1, ML на H4 |

---

## 13. Resilience Stack (полная картина)

| Механизм | Что защищает | AD/ADR |
|----------|-------------|--------|
| Circuit Breaker | Каскадный отказ при недоступности внешней системы | AD-8 |
| Retry (backoff+jitter, 1 слой) | Восстановление после транзиентного сбоя без перегрузки | AD-10, ADR-008 |
| DLQ | Poison message не блокирует партицию | AD-9, ADR-007 |
| Saga compensation | Частично выполненный платёж откатывается | AD-1, ADR-001 |
| Transactional Outbox | Расхождение БД ↔ Kafka | AD-2, ADR-002 |
| Idempotency (3 уровня) | Двойное списание при ретраях | AD-5, ADR-005 |
| Queue Load Leveling | Пиковая нагрузка 5–10× не валит процессинг | AD-17 |
| Rate Limiting | Один канал не заддосит остальных | AD-22 |
| Feature Flags (kill switch) | Экстренное отключение канала без деплоя | AD-24 |
| Disaster Recovery | Отказ ЦОД → RTO=15min, RPO=0 | AD-12, ADR-010 |
| Chaos Engineering | Валидация отказоустойчивости на практике | AD-25, ADR-016 |

---

## 14. Testing Strategy

| Уровень | Что покрывает | Инструмент | CI-гейт |
|---------|--------------|------------|---------|
| Unit | Бизнес-логика сервисов | Go testing, testify | Каждый PR |
| Contract | Совместимость consumer↔provider | Pact, schemathesis | Каждый PR (merge block) |
| Integration | Взаимодействие сервисов (БД, Kafka) | Testcontainers, Go | Nightly |
| E2E (skeleton) | Сквозной платёж | Go + docker-compose | Nightly + pre-release |
| Idempotency | Двойная отправка → один эффект | Custom test (двойной POST) | Каждый PR |
| Load | 5000 TPS, latency p99 | k6 / Gatling | Pre-release + weekly |
| Chaos | Отказоустойчивость | Chaos Mesh / Litmus | Staging daily + quarterly game day |
| DR Drill | RTO/RPO | Ручное переключение | Quarterly |

---

## 15. Deployment & Operations

### CI/CD Pipeline (stages)
1. **lint** — golangci-lint, форматирование
2. **unit-tests** — Go test, coverage > 80%
3. **contract-tests** — Pact + schemathesis (AD-23)
4. **schema-registry-check** — совместимость Kafka-схем (AD-15)
5. **build** — Docker image, tag = git SHA
6. **security-scan** — SAST (Semgrep), dependency scan (Trivy)
7. **deploy-staging** — auto-deploy to staging
8. **integration-tests** — nightly
9. **deploy-canary** — 5% → 25% → 50% → 100% (AD-16, ADR-013)
10. **rollback** — auto при error rate > 1% или manual

### Database Migrations
- Flyway, каждая миграция в транзакции, reversible.
- Expand-contract pattern (AD-16): expand → deploy → contract.
- Запрещены блокирующие операции на продакшене.
- Migration review — отдельный approval в PR.

### Feature Flag Workflow
- New feature → flag OFF → deploy → enable in staging → canary (flag 5%) → full (flag 100%).
- Kill switch: flag OFF → моментальное отключение канала.
- Flag audit: кто, когда, какой флаг (AD-26).

### DR Runbook (summary)
1. Detection: health probe fail × 3 → trigger.
2. Stateless: auto traffic shift (Istio) → DR cluster.
3. Stateful: manual PostgreSQL promote (standby → primary).
4. Kafka: consumer offset translation (MirrorMaker2).
5. Verification: data integrity check (row count, latest payment_id).
6. Traffic: 100% on DR.
7. Failback: reverse replication → verification → switch back.
8. Postmortem: update runbook, record RTO/RPO actual.
