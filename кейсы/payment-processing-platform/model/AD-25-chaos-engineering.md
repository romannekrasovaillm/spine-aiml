---
id: AD-25
type: ad
title: "Chaos engineering"
status: "ADOPTED"
affects: [CMP-002]
verified_by: [C-033]
---

- **Binds**: все компоненты ↔ chaos testing platform, game day schedule
- **Prevents**: отказоустойчивость не валидирована на практике; скрытые каскадные зависимости выявляются только в инциденте
- **Rule**: плановые game days (раз в квартал): (1) kill pod случайного сервиса, (2) network partition между зонами, (3) PostgreSQL failover, (4) Kafka broker down, (5) Redis unavailable, (6) Rail Connector timeout. Метрики: время обнаружения, время восстановления, affected transactions. Chaos — только в staging (или canary segment продакшена с 1% трафика). Результаты — в postmortem и обновление runbooks. Запрещён chaos в peak hours.
- **Статус**: [ADOPTED]
