---
id: AD-22
type: ad
title: "Rate limiting per channel/partner"
status: "ADOPTED"
affects: [CMP-001]
verified_by: [C-018]
---

- **Binds**: API Gateway ↔ все каналы (мобильный банк, Open API, партнёры)
- **Prevents**: один партнёр/канал заддосил процессинг → отказ для всех; отсутствует честное распределение ресурсов
- **Rule**: token bucket per tenant (channel + client_id): разные лимиты для разных каналов (банковский канал: 10000 RPS, партнёр: 100 RPS, Open API: 500 RPS). При превышении — 429 Too Many Requests + заголовок `Retry-After`. Приоритизация: банковский канал > партнёры (очередь/权重). Burst: 2× steady-state на 10 секунд. Алерт при стабильно высоком 429-rate для канала (возможно нужнаcapacity-ревизия). Конфигурация лимитов — без деплоя (DB-driven).
- **Статус**: [ADOPTED]
