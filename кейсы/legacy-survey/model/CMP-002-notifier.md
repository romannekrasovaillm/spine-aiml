---
id: CMP-002
type: cmp
title: "Notifier"
status: adopted
code_roots: [monolith/notifier, notifier]
depends_on: [CMP-001, INT-003]
---

Рассылка уведомлений. Читает ту же таблицу `payments` напрямую, без API
и без контракта — скрытая связь (notifier/sender.py:15); консьюмер очереди
`payments.notify` (notifier/sender.py:24). Тестов нет (карта, секция 6).
Зависимость от CMP-001 — через общую таблицу и очередь; declared, потому что
связь подтверждена картой (секция 5) и должна быть видна в модели, а не
только в SQL-строке.
