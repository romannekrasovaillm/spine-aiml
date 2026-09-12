# ADR-013. Стратегия развёртывания — canary и zero-downtime DB migrations

- Date: 2026-08-15
- Status: Accepted

## Context

Платформа процессинга — 99.99% availability (52 min/year downtime budget). Деплой с downtime недопустим. Миграции БД с блокировкой таблиц (`ALTER TABLE` lock) останавливают процессинг. Прямой выкат на 100% трафика — битый релиз уходит всем. Нужна стратегия для сервисов и БД.

## Decision

### Canary deployment (stateless сервисы)
- Выкат: 5% → 25% → 50% → 100% трафика (через Istio weighted routing).
- Окно наблюдения: 5 минут на ступень.
- Авто-rollback: при error rate > 1% или latency p99 > 2× baseline → автоматический откат.
- Ручной approve: переход 50% → 100% — ручной (контрольная точка).

### Blue-green (stateful: Orchestrator, Ledger)
- Blue (current) и Green (new) развёрнуты одновременно.
- Traffic switch: 100% Blue → 100% Green (через Istio).
- Откат: switch обратно (мгновенно, <30s).
- Green использует ту же БД (schema совместима — expand-contract).

### Zero-downtime DB migrations (expand-contract)
1. **Expand** (backward-compatible): `ALTER TABLE ADD COLUMN` (nullable, no default) — не блокирует. Новый код пишет в новую колонку, старый — игнорирует.
2. **Migrate** (background): заполнение колонки для существующих записей (batch update, без блокировки). Dual-write: новый код пишет и в старую, и в новую.
3. **Contract** (после деплоя нового кода): старая колонка больше не используется. `ALTER TABLE DROP COLUMN` — в окно низкого трафика (или не удалять, оставить deprecated).

- Миграции — через Flyway, каждая в транзакции, reversible (всегда есть down-миграция).
- Запрещены: `ALTER TABLE ... ALTER COLUMN ... TYPE` (блокировка), массовые `UPDATE` без batch.

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| **Rolling update** (K8s default) | Просто. | Нет наблюдения на малом трафике. Битый релиз уходит всем. |
| **Big-bang** | Быстро. | Downtime. Риск. |
| **Shadow deploy** | Тест на реальном трафике без эффекта. | Сложно для stateful. Double traffic. |

## Consequences

### Positive

- Zero downtime при деплое.
- Битый релиз обнаруживается на 5% трафика.
- Миграции БД не блокируют таблицы.

### Negative

- Canary — дополнительное время деплоя (5 ступеней × 5 мин = 25 мин).
- Expand-contract — две миграции вместо одной (expand → deploy → contract).
- Сложность: нужно поддерживать совместимость старой и новой версии кода одновременно (expand-период).

## Reversibility

**Reversible.** Переход на rolling — конфигурация deployment. Blue-green → canary — аналогично.

## References

- Spine: AD-16
- Связь: AD-14 (API versioning — совместимость)
