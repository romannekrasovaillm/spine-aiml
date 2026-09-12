# Платформа B2G-закупок (электронная коммерция для госсектора)

Проектирование платформы электронной коммерции для госсектора (B2G, 44-ФЗ/223-ФЗ): заказчик создаёт извещение → подписывает УКЭП → публикация в ЕИС → поставщик подаёт заявку с УКЭП → заявка принята, протокол. Маршрут **Critical** (Significance Score 13).

- Модель проектирования: **Kimi K3** (одна сессия; handoff-пакет сгенерирован позже — его `MANIFEST.json` фиксирует модель на момент генерации: `deepseek`).
- Ключевые документы: `ARCHITECTURE-SPINE.md` (7 инвариантов AD-1…AD-7), `docs/adr/ADR-001…005`, `docs/architecture/NFR.md`, `api/openapi.yaml`, `.arch-handoff/TASK.md`.
- Статус: дизайн готов, подготовлен handoff-пакет на **walking skeleton** (сквозной сценарий FZ44 с эмулятором ЕИС). Реализация не начата.

## Структура

```
ARCHITECTURE-SPINE.md        7 инвариантов AD-1…AD-7 (границы модулей, стек, outbox,
                             аудит, контракты, trust zone, криптография)
docs/
  adr/ADR-001…005.md         решения: workflow 44-ФЗ, on-prem, интеграция с ЕИС,
                             УКЭП/ГОСТ, модульный монолит
  architecture/NFR.md        измеримые NFR (p95, профиль «дедлайн», трансляция в ЕИС ≤ 5 мин)
  architecture/c4-container.mmd  C4-контейнеры (mermaid-исходник)
api/openapi.yaml             публичный контракт v0.1 (OpenAPI 3.1, 4 операции)
.arch-handoff/               пакет кодовому харнессу (этап: walking skeleton)
  TASK.md                    задача + критерии приёмки гейта A4 + headless JSON-контракт
  ARCHITECTURE.md            epic-context (дистиллят для исполнителя)
  CONSTRAINTS.yaml           fitness-правила для `arch-be control check`
  RUBRIC.yaml                якорная рубрика handoff_quality
  MANIFEST.json              модель, источники, бюджет epic-context
  adr/                       копии ключевых ADR на момент передачи
```

## Что смотреть в первую очередь

- `.arch-handoff/TASK.md` — образец скептичного к внешним системам дизайна: ЕИС только через outbox + адаптер (ретраи, circuit breaker, дедупликация), на skeleton — эмулятор со сбоями, а не реальный zakupki.gov.ru; тест устойчивости доказывает, что падение ЕИС не роняет приём заявок.
- `ARCHITECTURE-SPINE.md` AD-4/AD-7 — аудит append-only с hash-chain (запрет UPDATE/DELETE на уровне БД-роли) и криптография строго в модуле signing с честным маркером `FIXME-GOST` (SHA-256 на стенде вместо сертифицированного СКЗИ — с ссылкой на ADR-004).
- `docs/adr/ADR-005` — осознанный выбор **модульного монолита** (и on-prem, ADR-002) вместо микросервисов: контраст с микросервисным кейсом 002.
