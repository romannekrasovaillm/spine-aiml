---
id: QAS-001
type: qas
title: "Авторизация по картам в пике нагрузки"
status: "accepted"
implements: [NFR-001]
source: "клиент канала (мобильный банк)"
stimulus: "запрос авторизации карточного платежа в пике 5000 TPS"
artifact: "INT-001 Карточный рельс"
response: "ответ об авторизации возвращён без деградации приёма"
measure: "p99 < 2000 мс (NFR-001); hop INT-001 ≤ 800 мс — проверяется `arch-be nfr budget`"
---

Пиковый профиль из NFR-003 (5000 TPS). Деградация рельса не должна
просаживать приём: circuit breaker (AD-8) и queue load leveling (AD-17)
держат стимул в пределах бюджета.
