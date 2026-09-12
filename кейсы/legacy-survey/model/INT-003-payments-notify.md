---
id: INT-003
type: int
title: "Очередь payments.notify (RabbitMQ)"
status: accepted
contract: contracts/payments-notify.asyncapi.yaml
---

Транспорт уведомлений: `amqp://rabbit.internal.example:5672/`
(notifier/sender.py:9), консьюмер — CMP-002 Notifier
(basic_consume queue="payments.notify", notifier/sender.py:24).
Контракт зафиксирован ретроспективно по коду (событие PaymentRecorded);
producer в коде монолита не найден — вопрос владельцу домена (gap).
