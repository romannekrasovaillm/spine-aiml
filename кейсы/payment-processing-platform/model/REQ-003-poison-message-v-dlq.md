---
id: REQ-003
type: req
title: "Poison message уходит в DLQ"
status: "accepted"
---

Невалидное сообщение после 3 ретраев (1s, 5s, 25s) записывается в DLQ-топик с полным контекстом и не блокирует партицию.
