---
id: AD-9
type: ad
title: "Dead Letter Queue + стратегия ядовитых сообщений"
status: "ADOPTED"
affects: [CMP-002, CMP-009]
verified_by: [C-013]
---

- **Binds**: все Kafka consumers ↔ DLQ-топики `*.dlq`, операционная очередь ручной обработки
- **Prevents**: poison message блокирует партицию → стоп обработки платежей; невалидное сообщение повторяется бесконечно
- **Rule**: каждый consumer имеет политику retry: 3 попытки с экспоненциальной задержкой (1s, 5s, 25s), затем — запись в DLQ-топик с полным контекстом (message, error, stack trace, original offset). DLQ-топики мониторятся: алерт при росте > 0. Сообщения из DLQ обрабатываются оператором через отдельный инструмент (replay/reject/fix). Запрещён бесконечный retry. TTL сообщений в retry-топиках: 24ч.
- **Статус**: [ADOPTED]
