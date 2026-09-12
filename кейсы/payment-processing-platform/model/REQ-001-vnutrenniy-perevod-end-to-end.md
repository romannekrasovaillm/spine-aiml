---
id: REQ-001
type: req
title: "Внутренний перевод A→B end-to-end"
status: "accepted"
---

Сквозной сценарий walking skeleton H0: POST /payments → gateway → сага → холд → комиссия → двойная проводка → outbox → идемпотентный consumer. Критерии: go build/go test зелёные, баланс изменился один раз, событие получено один раз, audit log содержит все переходы.
