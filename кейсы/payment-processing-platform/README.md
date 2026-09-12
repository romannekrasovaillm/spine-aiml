# Платформа процессинга платежей (Greenfield)

Целевая архитектура центрального процессинга банка: единый узел для платёжных рельсов — карты (MIR/Visa/MC), СБП (C2C/C2B/B2C), SWIFT, БЭСП, внутренние переводы. Декомпозиция по bounded context (DDD), критический маршрут (Significance Score 13).

- Модель проектирования: **GLM-5.2** (одна сессия; handoff-пакет сгенерирован позже — его `MANIFEST.json` фиксирует модель на момент генерации: `deepseek`).
- Ключевые документы: `ARCHITECTURE.md` (целевая архитектура), `ARCHITECTURE-SPINE.md` (27 инвариантов AD-1…AD-27), `CONSTRAINTS.yaml` (34 fitness-правила под код), `docs/adr/ADR-001…016`.
- Типизированная модель `model/` (96 сущностей: SYS/CAP/CMP/INT/NFR/REQ/AD/ADR/RISK/OWNER со связями; ADR-003 в `docs/adr/` харнесса): `arch-be model validate model` — ссылочная целостность, `arch-be model show ADR-001 --dir model` — карточка, `arch-be model graph --dir model [--format mermaid]` — граф связей.
- Трассируемость (ADR-006): `arch-be trace check .` из корня кейса — покрытие звеньев REQ → NFR → AD/ADR → CMP → правило CONSTRAINTS.yaml; все 27 инвариантов спайна покрыты fitness-правилами (100%, PASS).
- Статус: дизайн готов, подготовлен handoff-пакет на **Walking Skeleton H0** (сквозной внутренний перевод A→B, Go/PostgreSQL/Redis/Kafka). Реализация не начата.

## Структура

```
ARCHITECTURE.md              целевая архитектура: рельсы, bounded contexts, NFR, риски
ARCHITECTURE-SPINE.md        27 инвариантов AD-1…AD-27 (Binds/Prevents/Rule, статусы)
CONSTRAINTS.yaml             34 fitness-правила под код (must_contain/must_not_contain, critical)
model/                       типизированная модель архитектуры (одна сущность = один .md
                             с YAML-frontmatter; ссылки depends_on/implements/affects/verified_by)
docs/adr/ADR-001…016.md      решения: сага-оркестрация, outbox, database-per-service,
                             trust-зоны, идемпотентность, state machine, DLQ, backoff,
                             сверка, DR (RTO 15мин/RPO 0), Vault/HSM, версионирование API,
                             zero-downtime миграции, async-границы, жизненный цикл данных
                             (152-ФЗ vs append-only аудит), chaos engineering
.arch-handoff/               пакет кодовому харнессу (этап: walking skeleton H0)
  TASK.md                    задача + критерии приёмки (исполнимые: go build/go test, SQL)
  ARCHITECTURE.md            epic-context (дистиллят для исполнителя)
  CONSTRAINTS.yaml           fitness-правила для `arch-be control check`
  RUBRIC.yaml                якорная рубрика handoff_quality
  MANIFEST.json              модель, источники, бюджет epic-context
  adr/                       проекция ADR из model/ (`arch-be model project model`), не редактировать
```

## Что смотреть в первую очередь

- `ARCHITECTURE-SPINE.md` — 27 обязательных инвариантов для параллельных исполнителей (сага только оркестрацией, outbox в одной транзакции, трёхуровневая идемпотентность, append-only аудит).
- `.arch-handoff/TASK.md` — образец исполнимой задачи: 9-шаговый сквозной путь, негативные критерии (повторный запрос, poison message → DLQ, 409 на недопустимый переход), план отката, headless JSON-контракт результата.
