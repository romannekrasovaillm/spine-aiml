---
id: CMP-001
type: cmp
title: "Billing"
status: adopted
code_roots: [monolith/billing, billing]
depends_on: []
---

Приём платежей HTTP `POST /charge` + `GET /health` (billing/worker.py:17,22),
расчёт комиссии и запись в общую таблицу `payments` (billing/worker.py:29).
Тариф — константа `BASE_COMMISSION_PCT = 1.2` (billing/tariffs.py:3);
флаг динамических тарифов мёртв с 2024-го (billing/worker.py:11).
Тесты есть только у этого компонента (billing/tests/).

Два корня координат (legacy-реальность): `monolith/billing` — путь файла
от корня кейса (владение файлом для context_boundary), `billing` —
координата python-импорта от корня запуска монолита (systemd-юниты,
crontab — cwd `monolith/`); ADR-030 допускает список корней.

Владелец кода: monolith/billing. Домен: биллинг/комиссии.
