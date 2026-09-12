---
id: AD-5
type: ad
title: "Идемпотентность на каждой точке входа"
status: "ADOPTED"
affects: [CMP-001, CMP-003, CMP-009]
verified_by: [C-001]
---

- **Binds**: Payment Gateway (API), все consumer'ы событий ↔ Redis (idempotency keys) + inbox-таблицы
- **Prevents**: двойное списание при ретраях клиента, дублирующая обработка при at-least-once доставке
- **Rule**: каждый mutating endpoint принимает `Idempotency-Key`. Дедупликация в **той же транзакции**, что и бизнес-эффект (inbox-таблица). Повтор возвращает ПЕРВЫЙ результат. TTL ключей = окно ретраев канала + буфер. Метрика: аномальный рост дублей = алерт.
- **Статус**: [ADOPTED]
