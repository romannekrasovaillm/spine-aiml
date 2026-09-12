# Headless-режим: arch-ml в скриптах и CI

Гид для разработчика/DevOps, встраивающего `arch-ml` в скрипты, пайпы,
Makefile, pre-commit и CI без SDK (или понимающего, что происходит под
капотом SDK). Все команды и сообщения ниже проверены живыми прогонами
`target/release/arch-ml` и чтением кода (`src/main.rs`, `src/llm.rs`,
`src/llm/openai_compat.rs`); пути в репозитории обозначены `<репо>`.

Первый запуск с нуля — `docs/getting_started.md`; здесь — только
неинтерактивный контур.

## 1. Философия: процесс как протокол

Headless — основной режим продукта в контуре банка: ночные сверки,
cron-задачи, CI-гейты, оркестрация скриптами ДИТ (ADR-020,
`docs/adr/ADR-020-headless-kontur-spine-be-edinyy-json-kontrakt-taymauty-chistyy-stdout-audit-treyl.md`).
Граница интеграции — не API и не RPC, а **процесс**: вызов `arch-ml`
читает stdin, пишет stdout, сообщает о сбое в stderr и возвращает exit
code. Скрипт видит ровно три канала:

| Канал | Контракт |
|---|---|
| stdout | Результат: финальный ответ агента (`run -q`), текстовый отчёт или JSON (`--json`-команды). Ничего лишнего — пригоден для `>` и пайпов. |
| stderr | Прогресс (в стрим-режиме) и причина сбоя (`Error: …` + цепочка `Caused by`). При успешном `run -q` — пуст. |
| exit code | 0 — успех; 1 — сбой процесса/проверки (причина в stderr); 2 — ошибка разбора аргументов (clap); в goal-режиме (`run --goal`) 0 — цель достигнута, 3 — blocked, 6 — paused (см. §2.5). |

Секреты через эту границу не ходят: ключи резолвятся лениво из
env/`api_key_file` (AD-3) и в stdout/stderr не попадают.

ADR-020 фиксирует и целевое развитие контракта: `arch run --json`
(единый объект `{status, summary, assumptions, open_questions,
session_log, elapsed_secs}` и коды 2/3/4 для blocked/partial/unknown) —
в текущей сборке `arch-ml run` флага `--json` **ещё нет** (проверено по
`run --help`; реализация идёт handoff-партиями по ADR-015). Действующий
контракт — разделы ниже.

## 2. `arch-ml run` — headless-прогон агента

Синтаксис из живого `arch-ml run --help`:

```text
Usage: arch-ml run [OPTIONS] [PROMPT]

Arguments:
  [PROMPT]  Промпт; `-` или отсутствие значения при пайпе — читать stdin

Options:
      --config <CONFIG>  Путь к config.toml
      --model <MODEL>    Модель (имя из [models])
      --no-stream        Без стриминга (печатать только финальный ответ)
  -q, --quiet            Строгий headless-контракт: stdout — только финальный
                         ответ; при успехе stderr пуст, при сбое — причина
                         в stderr и exit 1
      --timeout <SECS>   Общий таймаут прогона: по истечении — причина
                         в stderr и exit 1
      --max-turns <N>    Лимит итераций инструментов (перекрывает
                         agent.max_tool_turns из конфига)
      --think <on|off>   Ризонинг-режим: сливается карта thinking_on/off
                         из конфига модели; без флага — дефолт провайдера
```

### Источники промпта

Порядок разбора входа — `cmd_run` (`src/main.rs:1212`):

| Форма | Поведение |
|---|---|
| `arch-ml run "задача"` | Промпт — аргумент. |
| `cat spec.md \| arch-ml run -` | Явный `-` — читать stdin целиком. |
| `cat spec.md \| arch-ml run` | Без аргумента stdin читается, только если это не TTY (пайп/редирект). |
| `arch-ml run` в терминале | Без аргумента и пайпа — ошибка `нет промпта: передайте аргумент или пайп в stdin`, exit 1. |
| пустой ввод | Ошибка до запуска: `пустая задача: передайте непустой промпт аргументом или пайпом в stdin`, exit 1 (проверено: `< /dev/null`). |

### Промпт с ведущим `-`: сепаратор `--`

Промпт, начинающийся с дефиса, clap примет за флаг:

```text
$ arch-ml run -q --model deepseek "-внедрить feature-тоглы"
error: unexpected argument '-в' found

  tip: to pass '-в' as a value, use '-- -в'
# exit 2
```

Лечение — сепаратор `--` перед промптом (живой прогон: аргумент доходит
до агента, падает уже проверка модели):

```text
$ arch-ml run -q --model no-such-model -- "-внедрить feature-тоглы"
Error: llm: модель 'no-such-model' не настроена
# exit 1
```

### Контракт `-q` (строгий headless)

`-q/--quiet` форсирует нестриминговый режим (`stream = !no_stream && !quiet`,
`src/main.rs:797`): stdout несёт **только финальный ответ ассистента**,
события хода молчат. Эталон живого прогона —
`banking/demos/cli-from-claude-code/scenario1-headless/test-run.md`:
`run -q --model glm-5.3-flash --timeout 240 --max-turns 1 "…" > answer.md
2> stderr.log` — exit 0 за 2 мин 39 с, stderr пуст, `answer.md` — чистый
markdown-ответ (5 компонентов СБП-контура с NFR).

Без `-q` (стрим-режим) stdout тоже несёт только текст ответа (дельты), а
прогресс — вызовы инструментов `▶ tool: …`, `✓/✗ …`, «мысли» модели —
уходит в stderr (`src/main.rs:1279-1298`), пайп остаётся чистым. Разница
`-q` и `--no-stream`: оба печатают только финальный ответ, но `-q` — это
заявленный контракт (его соблюдение проверяется SDK), `--no-stream` —
просто переключатель вывода.

### Бюджеты прогона

- `--timeout SECS` — общий потолок (страховка CI/cron от зависшего
  провайдера). Превышение — `Error: таймаут прогона (Nс): провайдер или
  инструмент не ответил вовремя` (`src/main.rs:1320`), exit 1.
- `--max-turns N` — лимит итераций инструментов на этот прогон;
  перекрывает `agent.max_tool_turns` на клоне конфига, глобальный конфиг
  не трогается (`src/main.rs:2188-2196`).

### Коды выхода и типовые ошибки

| Exit | Ситуация | Пример stderr | Источник |
|---|---|---|---|
| 0 | Успех: ответ в stdout, stderr пуст | — | живой прогон (test-run.md) |
| 1 | Модель не настроена (падает **до сети**) | `Error: llm: модель 'no-such-model' не настроена` | `src/llm.rs:346`; живой прогон |
| 1 | Нет API-ключа провайдера | `Error: llm: провайдер '<имя>': нет API-ключа — установите <ENV> или положите ключ в файл …` | `src/llm/openai_compat.rs:476` |
| 1 | Провайдер ответил ошибкой | `Error: llm: <имя>: HTTP <status>: <первые 300 символов тела>` — 401 = ключ, 429 = лимит, 5xx = провайдер | `src/llm/openai_compat.rs:1221` |
| 1 | Провайдер молчит | `Error: llm: <имя>: таймаут: сервер не прислал заголовки ответа за Nс (перегрузка или сеть)` | `src/llm/openai_compat.rs:319` |
| 1 | Превышен `--timeout` | `Error: таймаут прогона (Nс): провайдер или инструмент не ответил вовремя` | `src/main.rs:2283` |
| 1 | Сетевой сбой (DNS/connect/reset) | `Error: error sending request …` + цепочка `Caused by` | `HarnessError::Http` (`src/error.rs:31`) |
| 1 | Невалидный `--think` | `Error: --think: ожидается on\|off, получено 'maybe'` | `src/main.rs:2182`; живой прогон |
| 2 | Ошибка разбора аргументов | `error: unexpected argument …` (текст clap, Usage в stderr) | живой прогон |
| 3 | Цель заблокирована: один блокер подтверждён `blocker_repeats` раз | `» цель приостановлена: …` (при сбое хода) либо заметка о блокере | `src/goal.rs` (`Decision::Blocked`); только `run --goal` |
| 6 | Цель на паузе: бюджет, лимит тернов, отсутствие прогресса, сбой хода | `» цель приостановлена: <причина>` | `src/goal.rs` (`Decision::Paused`); только `run --goal` |

Как различить без парсинга: exit 2 — всегда «вызывали неправильно» (чинить
команду); exit 1 + `модель … не настроена` — опечатка в `--model` (список —
`arch-ml models`); exit 1 + `нет API-ключа` — окружение CI; exit 1 +
`HTTP`/«таймаут» — провайдер или сеть, имеет смысл ретрай; exit 3/6 —
только goal-режим, состояние цели и причина в журнале сессии
(`{"event":"goal"}`), продолжить — `/goal resume`.

### Goal-режим в пайпе (`run --goal`)

`arch-ml run --goal "<что должно стать истиной> || <проверяемое состояние> ||
<команда проверки>"` — автономный прогон до механически проверенного
результата (`aiml/notes/goal-mode.md`). Промпт не нужен: первый терн петли
запускает цель, дальше харнесс сам продолжает работу — после каждого терна
проверяет критерий (`check`, exit 0) и, пока цель жива, вставляет в конец
контекста синтетическое `user`-сообщение с целью.

```bash
arch-ml run -q --goal "у 4 моделей есть eval_results.json || файлы на месте || test -f a.json && test -f b.json" > run.md
echo "exit=$?"   # 0 complete | 3 blocked | 6 paused
```

- **`0` complete** — критерий подтверждён командой `check` (exit 0). Самоотчёт
  модели «готово» цель не закрывает: суждение в петле не живёт.
- **`3` blocked** — один и тот же блокер подтверждён `[goal].blocker_repeats`
  раз; в журнале — причина.
- **`6` paused** — бюджет токенов, лимит тернов, серия пустых тернов или сбой
  хода (провайдер/сеть). Состояние заморожено, а не потеряно: продолжают
  `/goal resume` в TUI или повторный запуск.
- Ограничители петли — секция `[goal]` конфига; `--timeout` ограничивает
  прогон целиком (не один терн).

## 3. Пайп-паттерны

Ответ в файл (результат и прогресс/ошибки разведены):

```bash
arch-ml run -q --timeout 600 --max-turns 24 \
  "Составь чек-лист ревью ADR для платёжного контура" > answer.md 2> run.log
echo "exit=$?"   # 0 — answer.md полный; 1 — причина в run.log
```

Спецификация на входе (stdin-режим):

```bash
cat spec.md | arch-ml run -q --model deepseek-pro - > review.md
```

Обрыв пайпа — не сбой: `arch-ml run -q … | head` безопасен, печать финала
игнорирует `BrokenPipe` (`src/main.rs:1327-1331`).

Makefile:

```make
ADR ?= docs/adr

.PHONY: arch-review
arch-review:                # headless-ревью новых ADR
	arch-ml run -q --timeout 600 --max-turns 24 \
	  "Проверь ADR в $(ADR) на полноту разделов Context/Decision/Consequences" \
	  > arch-review.md 2> arch-review.log

.PHONY: gate
gate:                       # детерминированный гейт, LLM не нужен
	arch-ml control check . --json > fitness.json
```

pre-commit (`.git/hooks/pre-commit` или хук фреймворка — LLM-вызов на
коммит обычно тяжёл, поэтому в примере только детерминированный гейт;
`run -q` — по тому же шаблону, если нужен):

```bash
#!/bin/sh
# Красный гейт ломает коммит (exit 1); находки — в fitness.json для разбора.
arch-ml control check . --json > /tmp/fitness.json || {
  echo "arch-ml gate FAIL — см. /tmp/fitness.json" >&2
  exit 1
}
```

CI-шаг (условный): `arch-ml control check . --json > fitness-report.json`
артефактом; дальше `jq '.passed'`/`jq '.issues[]'` — схема отчёта в
`sdk/CONTRACT.md` §2.

## 4. Детерминированные команды без LLM

Работают без ключей и сети (сборка + конфиг; archify — ещё Node ≥18);
каждая — готовый CI-гейт. Профильные гайды — в последней колонке.

| Команда | Что делает | stdout | Exit-контракт | Гайд |
|---|---|---|---|---|
| `arch-ml control check <REPO> [--constraints F] [--json]` | Fitness-контроль репозитория по `CONSTRAINTS.yaml` | сводка «Правил/нарушений, Итог PASS/FAIL»; с `--json` — одна строка JSON `FitnessReport` | 0 — PASS, 1 — FAIL (JSON печатается и при FAIL) или ошибка исполнения | `docs/control.md` |
| `arch-ml archify validate\|deliver\|compare … [--json]` | Приёмка диаграмм JSON IR → HTML/SVG (9 checks + composition), атомарная доставка с SHA-256 receipt | сводка receipt; с `--json` — pretty JSON, `schemaVersion: 1` | 0 — `ok:true`; 1 — провал валидации/ошибка использования/таймаут (исходный код CLI в stderr) | `docs/archify.md` |
| `arch-ml mermaid <FILE\|->` | Черновой Unicode/ASCII-рендер mermaid (читается из stdin при `-`) | ASCII-арт диаграммы | 0 — рендер; 1 — файл не читается / ошибка разбора (`Error: mermaid: строка N: …`) | ADR-009 (`docs/adr/`) |
| `arch-ml kb <QUERY> [--limit N]` | Поиск по локальной базе знаний (`[knowledge].dirs`) | выжимки с `файл:строка` и score; нет совпадений — `Ничего не найдено.` | 0 — поиск выполнен (в т.ч. без совпадений, живой прогон); 1 — ошибка конфигурации kb | `docs/web_kb.md` |
| `arch-ml models` | Реестр настроенных моделей и дефолт | список «имя — модель (провайдер)» | 0 — реестр собран; 1 — ошибка конфигурации | `docs/models.md` |
| `arch-ml doctor` | 11 проверок окружения (ключи, каталоги, плагины, харнессы, MCP, archify, git) | отчёт `✓/⚠/✗` + итог | **1 при любом Fail**, иначе 0 (`src/doctor.rs:93-97`) — годится как health-гейт | `docs/getting_started.md` §4 |

Живые прогоны для таблицы (сводка):

```text
$ arch-ml control check . --constraints CONSTRAINTS.yaml --json   # зелёный репо
{"repo":".","passed":true,"issues":[],"summary":"Правил: 3, нарушений: 0 (error: 0, warn: 0)"}
# exit 0

$ arch-ml control check . --constraints CONSTRAINTS.yaml --json   # репо с находкой
{"repo":".","passed":false,"issues":[{"file":"src/hotfix.py","line":1,"rule":"no_pan_in_code", …}]}
# exit 1 — JSON при этом напечатан полностью: красный гейт — это данные, не сбой

$ arch-ml mermaid /tmp/no-such.mmd
Error: чтение /tmp/no-such.mmd

Caused by:
    No such file or directory (os error 2)
# exit 1
```

Два нюанса, важных для скриптов:

- `control check --json` при FAIL печатает **валидный JSON и возвращает
  1**: `set -e`/`||`-обвязка не должна считать это потерей отчёта.
- `archify` при провале тоже печатает JSON-receipt с `ok:false` и
  `diagnostics` в stdout — парсить можно всегда, независимо от exit.

## 5. Планировщик: cron

Повторяющиеся headless-задачи (ночные сверки, дайджесты базы знаний,
дрейф-чеки спецификаций) оформляются не обёрткой над `run`, а штатным
планировщиком `arch-ml cron`: задача — markdown-инструкция исполнителю,
расписание — 5-полевое cron-выражение в `cron.toml`, периодичность —
системный cron, вызывающий `arch-ml cron tick`; результат — md-отчёт
`<out>/<name>-<timestamp>.md`, последняя строка которого — JSON-статус
`{"status": "complete|partial|blocked", "summary": "…"}` (разбор —
`tail -1 report.md | jq .status`). Конфигурация, `tick`, примеры
инструкций и отбор отчётов по статусу — `docs/cron_and_md_pipes.md`.

## 6. Отладка: что смотреть при сбое

Порядок разбора упавшего headless-прогона:

1. **stderr прогона** — первичный источник: таблица ошибок §2 покрывает
   типовые причины; цепочка `Caused by` даёт исходную ошибку ОС/сети.
2. **`--no-stream` вместо `-q`** при ручной диагностике не нужен — наоборот:
   снимите `-q` и перенаправьте stderr в файл, чтобы видеть прогресс
   (`▶ tool: …`, `✓/✗ …`): видно, на каком вызове инструмента всё встало.
3. **`arch-ml doctor`** — если подозрение на окружение (ключи, каталоги,
   archify, MCP): exit 1 при любом Fail; для контура диаграмм отдельно
   `arch-ml archify doctor`.
4. **Журнал сессии** — полный аудит-трейл прогона (что агент реально
   вызывал): append-only JSONL `session-<yyyymmdd-hhmmss>-<pid>[-n].jsonl`
   в `paths.sessions_dir`, по умолчанию `~/.arch-ml/sessions/`
   (корень — `$ARCH_ML_HOME` или `~/.arch-ml`; `src/config.rs:703-713`,
   `src/agent.rs:1227-1244`). Создаётся на каждый `run`, включая упавшие.
5. **`arch-ml metrics`** — агрегаты по журналам сессий и отчётам
   (повторяемость сбоев, расход), когда проблема не разовая.

## 7. Граница с SDK: когда достаточно CLI

SDK (Python/Rust/Java) — тонкие клиенты поверх этого же headless CLI:
запускают процесс без shell, читают те же stdout/stderr/exit и разбирают
JSON-контракты (`sdk/CONTRACT.md`, ADR-028). Всё из этого гайда остаётся
в силе — SDK лишь типизирует его на языке команды.

| Ситуация | Достаточно CLI | Берите SDK |
|---|---|---|
| Одноразовые скрипты, Makefile, cron-обвязка | ✓ | |
| CI-гейт: коды 0/1 + `jq` по JSON-отчёту | ✓ | |
| Потребитель — код на Python/Rust/Java (сервис, бот, кодовый агент) | | ✓ типизированный `FitnessReport`/receipt, без ручного парсинга |
| Нужны таймаут с убийством процесса и устойчивость к «мусору» в stdout | частично (shell-обвязка) | ✓ клиентский таймаут, `ContractViolation`, `ProcessFailed` с `exitCode`/`stderr` |
| Различение «красный гейт» и «сбой исполнения» в логике вызывающего | вручную (exit + наличие JSON) | ✓ `CheckFailed` — данные, не исключение |
| Кросс-языковая команда (Python + Rust + Java) | | ✓ одна форма API и ошибки, parity-прогон |

Полное описание — `docs/sdk.md`, контракт — `sdk/CONTRACT.md`.

## См. также

- `docs/getting_started.md` — установка, конфиг, первый прогон (§6 — тот же контракт `-q`).
- `docs/tui.md` — интерактивная поверхность: когда диалог удобнее скрипта.
- `docs/cron_and_md_pipes.md` — планировщик и баш-пайпы (прогресс в stderr, `--timeout`/`--max-turns`).
- `docs/control.md` — схема `CONSTRAINTS.yaml` и типы проверок fitness.
- `docs/archify.md` — контур диаграмм: `--json`-receipt, коды выхода, quality-профили.
- `docs/models.md` — реестр моделей и настройка провайдеров.
- `docs/web_kb.md` — база знаний: настройка `[knowledge]` и поиск.
- `docs/sdk.md` + `sdk/CONTRACT.md` — SDK и машиночитаемые контракты v1.
- `docs/adr/ADR-020-headless-kontur-spine-be-edinyy-json-kontrakt-taymauty-chistyy-stdout-audit-treyl.md` — решение по headless-контуру (целевой `run --json`, таймауты, аудит-трейл).
- `banking/demos/cli-from-claude-code/scenario1-headless/test-run.md` — эталон живого прогона `run -q` (проприетарная зона).
