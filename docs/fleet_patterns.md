# Паттерны оркестрации флотов кодовых агентов (план флота)

Передача архитектуры в работу — не всегда веер. `arch-ml fleet` исполняет
**план флота**: декларативный граф узлов, где паттерн — это сахар над графом,
компилятор достраивает недостающие узлы и рёбра, а дальше идёт один волновой
прогон по топологическим уровням (ADR-042).

```
fleet plan propose --repo .          # черновик плана по handoff-пакету
fleet plan validate <plan>           # механическая проверка (exit 1 при ошибке)
fleet plan show <plan> [--mermaid]   # волны, роли, гейты, зависимости
fleet run --repo . --plan <plan>     # прогон
fleet resume <run-id> [--repo .]     # продолжение из журнала
fleet merge <run-id> --owner-approve # интеграция в main (только владелец)
```

## Каталог паттернов

| Паттерн | Топология (достраивает компилятор) | Судья | Когда брать |
|---|---|---|---|
| `fanout` | N worker без рёбер | нет (мерж — владелец) | независимые эпики, кейс 005 |
| `pipeline` | цепь по `order` (или `depends_on`) | гейт каждой стадии | спека → скелет → тесты → приёмка |
| `map_reduce` | N worker + узел `integrator` на все | интеграционный гейт | склейка N результатов в один пакет |
| `tournament` | N worker с `group` + узел `judge` | детерминированный: первый по id, прошедший гейты | лучший из N попыток |
| `review_pair` | worker + узел `reviewer` + гейт `review` | состязательный ревьювер READY/NOT-READY | код идёт в main — нужен независимый глаз |
| `ralph` | один узел `role = ralph` | `RalphHandoff.status` | цель, которую надо «дожать» раундами |
| `walking_skeleton` | узел-скелет → остальные зависят от него | гейт скелета + `stop_on_gate_fail` | Critical: сначала canary, потом масштаб |
| `dag` | ничего (явные `[[edges]]`) | гейты по узлам | произвольный граф (супермножество) |

**Судья всегда детерминированный.** Вердикт ревьювера (LLM) принимается
только для *блокировки* интеграции и никогда — для выбора «лучшего кода»:
выбор в турнире делает порядок гейтов, а не модель.

## Схема плана

```toml
id = "bankcalc-mapreduce"
package = "bankcalc"
pattern = "map_reduce"          # компилятор достроит узел reduce
repo = "."

[policy]
max_parallel = 16               # потолок одновременных процессов
stop_on_gate_fail = true        # отказ гейта останавливает волны
require_worktree = true         # нет пути в main: прогон только в worktree
merge_gate = "owner"
autonomy = "R2"                 # R-уровень для Command-гейтов
[policy.budget]
max_node_secs = 3600
max_attempts = 3

[defaults]
route = "standard"
timeout_from_manifest = true    # таймаут из .arch-handoff/MANIFEST.json
gates = ["contract", "fitness"]
on_fail = { retry = 1 }

[[nodes]]
id = "svc-a"
spec = "Реализовать сервис A по SPEC.md пакета."
spec_files = ["ARCHITECTURE-SPINE.md"]
required_skills = ["python"]
domain = "code"
outputs = ["svc_a/__init__.py"]
```

Формат: TOML — канон, JSON и YAML принимаются. План живёт в
`<repo>/.arch-fleet/<id>.plan.toml` (коммитится и ревьюится в PR); состояние
прогона — в `~/.arch-ml/state/fleet/`, рядом с журналом лежит снимок плана.

### Поля узла

`id`, `spec`, `spec_files[]`, `package`, `required_skills[]`, `tags[]`,
`route` (`fast|standard|critical`), `domain`, `effort`, `role`
(`worker|skeleton|integrator|reviewer|judge|ralph`), `depends_on[]`, `group`,
`gates[]`, `on_fail` (`block|skip|continue` или `{ retry = N }`),
`timeout_secs`, `outputs[]`, `agent`, `max_rounds`.

### Гейты

| Гейт | Проверка |
|---|---|
| `contract` | JSON-контракт результата (`status` в `allow`, дефолт `complete`) |
| `fitness` | `control check` по `CONSTRAINTS.yaml` |
| `spine_lint` | линтер `ARCHITECTURE-SPINE.md` |
| `delta_guard` | правки защищённых путей только через активную дельту |
| `outputs` | объявленные артефакты существуют |
| `command` | детерминированная команда, exit 0 (тесты) |
| `review` | вердикт ревьювера READY/NOT-READY |

Короткие формы — строкой (`"contract"`, `"fitness"`), параметризованные —
таблицей: `{ command = { cmd = "pytest -q", timeout_secs = 600 } }`,
`{ outputs = { paths = ["a.py"] } }`. `command` проходит классификацию
политики (R-уровни): деструктивная команда на текущем уровне получает `warn`
с объяснением, а не исполняется молча.

## Масштаб: сотни узлов

Флот из сотен узлов в плане исполняется **пулом**: конкурентность ограничена
`policy.max_parallel` (`tokio::sync::Semaphore`), остальные узлы ждут слота
(событие `node_queued`). Движок без потолка не спавнит — потолок ставит
владелец: процессы кодовых харнессов тяжёлые, а реальный регулятор при
больших потолках — лимиты провайдера.

Что ещё важно на масштабе:

- **Диск.** worktree-на-узел: каталоги вне репозитория
  (`~/.arch-ml/worktrees/<repo-slug>/<run-id>-<node>`), имя детерминировано —
  resume переиспользует дерево, а не плодит копии. Чистка — `fleet audit`
  (рекомендация prune старше 30 дней).
- **Предупреждение ёмкости.** `fleet plan validate` предупреждает, если
  `max_parallel` заведомо выше разумного для машины, и если узлов больше сотни.
- **Журнал.** Append-only JSONL, одна строка — один `write_all`: дописи
  конкурентных узлов не склеиваются (регрессия зафиксирована тестом).
- **Управление на лету.** `fleet control <run-id> --agent <узел|*>`
  (`--kill|--pause|--resume`) — тот же control-канал, что у веера.

## Resume

`fleet resume <run-id>` читает журнал и исключает завершённые узлы из
расписания: повторный прогон догоняет только незавершённое. Идемпотентность
обеспечена детерминированным именем worktree. Для прогонов без
worktree-изоляции resume отказывается работать без `--force-rerun` —
повторный прогон узла лёг бы поверх уже сделанной работы.

## Честные границы

- Движок **не декомпозирует** пакет на узлы — это работа архитектора
  (предложение `fleet plan propose` несёт черновик, узлы дописывает человек).
- **Не мержит в main**: интеграция — только `fleet merge --owner-approve`.
- **Не судит качество LLM-судьёй**: вердикт ревьювера только блокирует.
- **Не enforce'ит область чтения** ревьювера на уровне ОС для subprocess-
  харнессов (для in-process субагента — whitelist инструментов).
- **Не считает деньги**: харнессы не рапортуют токены/₽, бюджет — только в
  наблюдаемых единицах (`max_node_secs`, `max_attempts`, `max_parallel`).
- **Не заменяет** `fleet audit` (SSOT-аудит дублей и дрейфа копий спайна).
- **Не гарантирует бесконфликтный resume**: повторный прогон узла накапливает
  коммиты в ту же ветку — конфликт решает владелец.

Связанное: [fleet.md](fleet.md) (worktree-изоляция и owner-гейт),
[handoff_walkthrough.md](handoff_walkthrough.md) (пакет → прогон),
[control.md](control.md) (fitness-функции и линтер спайна),
ADR-042 (решение), `кейсы/fleet-of-ten` (живой веер из десяти агентов).
