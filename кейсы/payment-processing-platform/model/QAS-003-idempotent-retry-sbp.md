---
id: QAS-003
type: qas
title: "Идемпотентный повтор оплаты СБП"
status: "accepted"
implements: [NFR-002]
source: "ТСП (клиент СБП C2B)"
stimulus: "повторный запрос оплаты с тем же Idempotency-Key"
artifact: "CMP-001 Payment Gateway"
response: "возвращён исходный результат, повторного списания нет"
measure: "ровно одно списание в ledger; p99 ответа < 500 мс (NFR-002)"
---

Идемпотентность на каждой точке входа (AD-5): повтор доезжает из кэша
ключей, не доходя до саги. Негативный сценарий walking skeleton H0
(REQ-001) покрывает это приёмкой `go test`.
