---
id: REQ-002
type: req
title: "Идемпотентный повтор запроса"
status: "accepted"
---

Повторный POST с тем же Idempotency-Key возвращает тот же payment_id и первый результат, без повторного бизнес-эффекта.
