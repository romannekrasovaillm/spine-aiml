---
id: SYS-001
type: sys
title: "Платформа процессинга платежей"
status: "designed"
---

Центральный узел банка для платёжных рельсов: карты (MIR/Visa/MC), СБП (C2C/C2B/B2C), SWIFT, БЭСП, внутренние переводы. Декомпозиция по bounded context (DDD), критический маршрут (Significance Score 13). Каналы: мобильный банк, веб-банк/ДБО, Open API, ATM/POS.
