---
id: ADR-010
type: adr
title: "Disaster Recovery — active-passive, RTO=15min, RPO=0"
status: "Accepted"
date: "2026-08-15"
implements: [AD-12]
affects: [CMP-002, CMP-007]
---

## Context

Платформа процессинга — объект КИИ (187-ФЗ). Отказ ЦОД = остановка платежей в масштабе банка. NFR: RTO=15min, RPO=0 (без потери данных). Без явной DR-стратегии — надежда на то, что «ничего не случится», что не является инженерным подходом.

## Decision

**Active-passive с hot standby DR-сайтом.**

### Топология
- **Primary ЦОД**: active. Весь трафик. Все сервисы — active.
- **DR ЦОД**: hot standby. Сервисы развёрнуты, но не принимают трафик. Данные — синхронно реплицированы.

### Репликация
| Компонент | Метод | RPO |
|-----------|-------|-----|
| PostgreSQL | Synchronous streaming replication (synchronous_commit=on, synchronous_standby_names) | 0 |
| Kafka | MirrorMaker2 (sync mode, acks=all, min.insync.replicas=2) | ~0 (синхронный) |
| Redis | Replica (async) — кэш, перестраивается | N/A (кэш) |
| Kubernetes | Отдельный кластер в DR, развёрнут через тот же GitOps | — |

### Failover
1. **Detection**: health probe на primary → 3 последовательных fail → автоматический trigger failover.
2. **Stateless сервисы (K8s)**: автоматический — traffic shift через DNS/ingress (Istio).
3. **Stateful (PostgreSQL)**: ручное promote standby → primary (автоматическое — риск split-brain).
4. **Kafka**: consumer groups переключаются на DR-кластер (offset translation через MirrorMaker2).
5. **Время**: stateless — <2min, stateful — <10min (PostgreSQL promote + verification). Итого RTO < 15min.

### Failback
- Ручная процедура: (1) репликация DR → primary (reverse), (2) verification (сверка данных), (3) traffic shift обратно, (4) primary → standby.
- Runbook: пошаговая инструкция, timeout на каждый шаг.

### DR Drill
- Раз в квартал: плановое переключение на DR-сайт (с полным трафиком на 1 час).
- Метрики: `dr_drill_rto_actual`, `dr_drill_rpo_actual`, `dr_drill_data_integrity_check`.
- Результаты — в postmortem и обновление runbook.

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| **Active-active** | RTO≈0. Нет переключения. | Конфликт-резолюция для финансового состояния (split-brain). Сложность. Запрещено для single-writer (AD-4). |
| **Cold standby** | Дёшево. | RTO = часы (развертывание + восстановление). Не соответствует NFR. |
| **Cloud DR** | Без капитальных затрат. | Данные КИИ в облаке — ограничения 187-ФЗ. Требуется сертификация. |

## Consequences

### Positive

- RTO=15min, RPO=0 — соответствие NFR и 187-ФЗ.
- Плановая валидация (DR drill) — уверенность в восстановлении.
- Hot standby — минимальное время переключения.

### Negative

- Стоимость: второй ЦОД, серверы, лицензии (не используются в normal mode).
- Синхронная репликация — дополнительная задержка записи (~1–5ms на COMMIT).
- Ручное failover для PostgreSQL — операционный риск (митигация: runbook, drill).

## Reversibility

**Costly.** Переход на active-active — фундаментальное изменение (конфликт-резолюция, multi-writer). Практически — новый проект.

## References

- Spine: AD-12
- NFR: RTO=15min, RPO=0
- Регулятор: 187-ФЗ (КИИ)
