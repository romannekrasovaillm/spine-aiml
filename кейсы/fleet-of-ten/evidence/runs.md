# Фактические исходы прогонов флота (2026-08-16, вечер)

Стенд: оркестрация через CLI Spine (`arch-be handoff` + `arch-be harness-run`);
исполнители — 10 × Claude Code headless (`claude -p
--dangerously-skip-permissions`, бэкенд `deepseek-v4-pro[1m]`), каждый в
изолированном git-worktree с под-пакетом `.arch-handoff/` (epic-context из
спайна, per-module CONSTRAINTS.yaml). Спайн `ARCHITECTURE-SPINE.md`
(AD-1…AD-10) — единственный канал согласования.

Стена: все 10 стартовали одновременно (tmux), последний финишировал на
191-й секунде — **десять эпиков за ~3,2 минуты стены**.

| Прогон | Эпик (инвариант) | Код | Длит. | Тесты | Fitness (6 правил) | Контракт | Коммит |
|---|---|---|---|---|---|---|---|
| p01-amount | `validate_amount` (AD-1) | 0 | 103.7 с | 5 passed | PASS | complete, conflicts 0 | сам исполнитель |
| p02-money | `round_money` (AD-2) | 0 | 141.3 с | 4 passed | PASS | complete, conflicts 0 | сам исполнитель |
| p03-fee | `calc_fee` (AD-3) | 0 | 103.2 с | 4 passed | PASS | complete, conflicts 0 | сам исполнитель |
| p04-rate | `convert` (AD-4) | 0 | 178.4 с | 4 passed | PASS | complete, conflicts 0 | сам исполнитель |
| p05-ids | `payment_id` (AD-5) | 0 | 126.3 с | 4 passed | PASS | complete, conflicts 0 | сам исполнитель |
| p06-calendar | `is_weekend` (AD-6) | 0 | 141.8 с | 5 passed | PASS | complete, conflicts 0 | сам исполнитель |
| p07-retry | `backoff` (AD-7) | 0 | 154.9 с | 4 passed | PASS | complete, conflicts 0 | сам исполнитель |
| p08-tax | `vat` (AD-8) | 0 | 189.9 с | 4 passed | PASS | complete, conflicts 0 | сам исполнитель |
| p09-limit | `daily_limit_key` (AD-9) | 0 | 153.9 с | 4 passed | PASS | complete, conflicts 0 | сам исполнитель |
| p10-summary | `summarize` (AD-10) | 0 | 191.4 с | 4 passed | PASS | complete, conflicts 0 | сам исполнитель |

**Итого: 10/10 complete, 42/42 тестов, 60/60 fitness-правил, 10/10
коммитов исполнителями.**

## Отличие от кейса 004 (тот же день, утро)

Утренний флот из трёх исполнителей завершился «complete», но **без единого
коммита** — контракт его не требовал, и интеграцию собирал оркестратор
вручную. К вечернему флоту в TASK.md уже была секция «Финализация
(обязательно)»: все десять исполнителей закоммитили работу сами
(`evidence/commits.txt`), авто-коммит харнесса (страховка) не сработал ни
разу — он и не должен был. Контракт разобран механически
(`parse_result_contract`): строка `контракт: status=complete …` в каждом
выводе.

## Интеграционный гейт

Склейка десяти модулей в единый пакет `bankcalc` — сквозной сценарий
«день процессинга» (платёж проходит все десять контрактов по цепочке) —
прошёл с первой сборки: `INTEGRATION OK` (см. `integration-run.txt`),
`spine_lint` — «нарушений нет». Замечание о честности: первая редакция
сквозного теста упала на арифметике ожидания самого проверяющего
(11025.75 ≠ 11029.5) — модуль был прав, тест исправлен; зафиксировано,
потому что «гейты зелёные с первого раза» без таких оговорок были бы
нечестными.
