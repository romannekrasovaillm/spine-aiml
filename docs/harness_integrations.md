# Интеграция кодовых харнессов

Передача архитектуры в код: arch-harness готовит **handoff-пакет** для
кодового агента (Claude Code, Qwen Code, OpenClaw, Hermes, Theseus,
CodeWhale), прогоняет его и контролирует результат. Реализация —
`src/harness.rs`; идеи — BMAD epic-context и headless-контракты
(`docs/SOURCE_BRIEF.md` §A.3).

## Handoff-пакет `.arch-handoff/`

`arch-ml handoff` создаёт в корне репозитория каталог `.arch-handoff/`. Перед
сборкой срабатывает **предгейт git**: каталог без репозитория получает
`git init` + пустой baseline-коммит (якорь отката; содержимое в baseline не
подмётается — это зона исполнителя), у git-репозитория якорем становится
текущий HEAD. Без git контракт финального коммита невыполним, поэтому пакет
без якоря собирается с предупреждением.

| Файл | Содержимое | При повторной генерации |
|---|---|---|
| `TASK.md` | Задача + **план отката** (якорь baseline, сигналы-триггеры, владелец решения) + **финализация** (обязательный git-коммит) + контракт результата (ниже). | Перезаписывается. |
| `ARCHITECTURE.md` | **Epic-context** — дистиллят спек/спайна. | Перезаписывается. |
| `CONSTRAINTS.yaml` | Fitness-правила для `arch-ml control check`. | Не затирается (пишется дефолт при отсутствии). |
| `RUBRIC.yaml` | Рубрика приёмки (копия якорной `handoff_quality`). | Не затирается. |
| `adr/` | Копии переданных ADR-файлов (`--spec`, признак: `ADR` в имени или `/adr/` в пути). | Существующие копии не затираются. |
| `MANIFEST.json` | Мета: `created_at` (UTC), `task`, `model` (default_model), `sources`, `epic_context_chars`, `epic_context_tokens`, `route` (Fast/Standard/Critical), `recommended_timeout_secs` (1800/3600/7200 — подхватывается `harness_run`, если `timeout_secs` не задан явно). | Перезаписывается. |

Граница окна рубрики: при маршруте **Critical** epic-context ниже ~800
токенов — отказ на сборке пакета (добавьте `--spec` или осознанно понизьте
маршрут); Standard — предупреждение, Fast — допустимо. Если на момент
генерации есть незакоммиченные изменения отслеживаемых файлов, пакет
предупреждает: откат на baseline (`git reset --hard`) их потеряет.

### TASK.md и headless JSON-контракт

Перед финальным ответом исполнитель обязан зафиксировать работу в git
(секция «Финализация» генерируемого TASK.md): `git add -A -- .
':!.arch-handoff'` + `git commit` — результат забирается оркестратором из
git log, работа без коммита считается невыполненной. Страховка на стороне
харнесса — **авто-коммит** (`auto_commit = true` по умолчанию): после
успешного прогона незакоммиченные правки исполнителя фиксируются коммитом
`harness(<имя>): <первая строка задачи>` (кроме `.arch-handoff/` и мусора
`__pycache__/`/`*.pyc`/`.pytest_cache/`; идентичность коммита —
`spine-harness`, чтобы не зависеть от user.name в окружении). Факт
авто-коммита помечается в сводке прогона.

Финальный ответ кодового харнесса обязан завершаться JSON-объектом
(после него — ни символа):

```json
{"status": "complete|partial|blocked", "assumptions": [], "open_questions": [], "conflicts_with_prior_decisions": []}
```

- `status`: `complete` — выполнено полностью; `partial` — частично;
  `blocked` — заблокировано.
- `assumptions` — допущения, принятые при реализации.
- `open_questions` — вопросы к архитектору.
- `conflicts_with_prior_decisions` — расхождения с принятыми решениями
  (ADR, spine); конфликт со spine обязан останавливать работу, а не
  «решаться на месте».

Каналы приёмки контракта (по приоритету): файл `.arch-handoff/result.json`
(устойчив к обрыву стрима и потере fence; перед прогоном старый файл
удаляется — свежий гарантированно этого прогона), затем stdout. Требование
контракта добавляется каноническим футером к задаче КАЖДОГО прогона (ad-hoc
задачи не несут TASK.md; если задача задаёт свой формат JSON-ответа — его
поля объединяются в тот же объект, обязательные поля сохраняются).

**Механический разбор на стороне запуска** (`parse_result_contract`):
контракт — не текстовая инструкция, а валидируемая схема. Сканируются
fenced ```json-блоки с конца вывода (тег валиден только перед переводом
строки — упоминание «```json» в прозе блоком не считается; блок со
`status`, не парсящийся как JSON, — это `Invalid`, а не промах) плюс
запасной путь — голый JSON-объект в хвосте (модели иногда роняют fence);
перед разбором снимаются ANSI-последовательности. `status` строго из
complete|partial|blocked; списки опциональны, но если присутствуют —
обязаны быть массивами. Итог типизирован (`ContractParse`:
Valid/Invalid/Missing) и живёт в `HarnessRun.contract`: сводка
`harness_run` эскалирует `blocked` и непустые conflicts/open_questions
списками, CLI `arch-ml harness-run` печатает машинную строку
`контракт: status=… assumptions=N open_questions=N conflicts=N` и на
`status=blocked` завершается кодом **2** (скриптовый гейт в пайпах).

**Дозапрос-ремонт.** Нормально завершённый прогон без валидного контракта
получает один bounded раунд (≤ 5 минут): исполнителю шлётся точная причина
отказа + хвост его вывода с требованием выдать ровно блок по схеме (по ACP —
в той же сессии, процессный транспорт — отдельным коротким запуском).
Восстановленный контракт помечается в сводке «(восстановлен дозапросом)»,
факт раунда виден в логе; источник контракта фиксируется
(`ContractOrigin`: stdout / result.json / дозапрос).

### ARCHITECTURE.md (epic-context, 800–1500 токенов)

Компиляция из `--spec`-файлов (`compile_epic_context`): секции с полями
`Binds:`/`Prevents:`/`Rule:` (ADR-блоки spine) включаются **целиком**,
прочие секции — заголовок + первые абзацы. Глубина адаптивная: старт — 2
абзаца на секцию, но если итог недобирает до ~800 токенов (низ окна рубрики
handoff_quality), спеки перерендериваются глубже (до 8 абзацев) — сценарий
«реализация без доступа к источникам» требует массы. Сверху итог усечён до
6000 символов (≈1500 токенов при оценке 4 символа/токен) с пометкой об
усечении. `handoff_create` предупреждает в выводе, если epic-context всё же
ниже окна (мало источников — добавьте `--spec`). Смысл: агент реализует без
доступа к исходным документам и без архитектурных изобретений.

## Цикл «передача → прогон → контроль»

```bash
# 1. Передача: генерация пакета (спеки/спайн/ADR — через --spec)
arch-ml handoff claude-code --repo ~/work/payment-svc \
  --task "Реализуй платёжный оркестратор по ADR-001 и ADR-002" \
  --spec docs/ARCHITECTURE-SPINE.md --spec docs/adr/ADR-001-orchestrator.md

# 2. Прогон: бинарь харнесса с задачей (по умолчанию — текст .arch-handoff/TASK.md)
arch-ml harness-run claude-code --repo ~/work/payment-svc

# 3. Контроль: fitness functions по CONSTRAINTS.yaml (exit 1 при FAIL)
arch-ml control check ~/work/payment-svc

# 4. Приёмка пакета/результата рубрикой (LLM-судья)
arch-ml rubric run handoff_quality ~/work/payment-svc/.arch-handoff/TASK.md
```

Известные харнессы — `arch-ml harnesses` (показывает бинарь, режим промпта и
наличие в PATH). В TUI: `/handoff <harness> <repo> [task...]`,
`/control <repo>`.

## Настройка `[harnesses.*]`

```toml
[harnesses.claude-code]
binary = "claude"                 # имя бинаря в PATH
args = ["-p"]                     # дополнительные аргументы
prompt_mode = "stdin"             # positional | flag | stdin
timeout_secs = 1800               # абсолютный потолок прогона
idle_timeout_secs = 600           # таймаут тишины: 0 — выключить
auto_commit = true                # до-коммитить незакоммиченные правки исполнителя
# transport = "acp"               # протокольный транспорт ACP v1 (см. ниже)
# acp_session = "reviewer"        # алиас продолжаемой сессии (опционально)
# acp_permission = "readonly_auto" # readonly_auto (дефолт) | deny | allow_all
# env_allow — whitelist наследуемого окружения: процесс стартует с чистым
# env и получает только перечисленное + env адаптера (закрывает утечку
# окружения хоста в харнесс: чужие *_MODEL, прокси, ключи). Пустой — как раньше.
# env_allow = ["PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR",
#              "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"]
# env = { FOO = "bar" }           # дополнительные переменные окружения
```

`prompt_mode` (`src/config.rs::PromptMode`, сборка argv — `build_argv`):

- `positional` — задача добавляется последним аргументом: `binary args... "задача"`;
- `flag` — в `args` плейсхолдер `{prompt}` заменяется задачей (без
  плейсхолдера задача добавляется последним аргументом);
- `stdin` — задача пишется в stdin (писатель — отдельной задачей, без
  дедлока на pipe-буфере), argv = `binary args...`.

## Транспорт ACP (`transport = "acp"`, ADR-049)

Процессный транспорт видит харнесс чёрным ящиком (ответ — stdout по итогу,
контекст между прогонами теряется). Для харнессов с поддержкой Agent Client
Protocol v1 (проверено живым handshake: `kimi acp`) есть протокольный
режим — ndjson JSON-RPC поверх stdio:

```toml
[harnesses.kimi-acp]
binary = "kimi"
args = ["acp"]
transport = "acp"
# acp_session = "reviewer"        # см. политику контекста ниже
# acp_permission = "readonly_auto"
```

Что даёт прогон (`initialize` → `session/new`|`session/resume` →
`session/prompt` → `session/close`):

- **Видимость хода.** `session/update` проецируется в лог прогона:
  tool-вызовы (`[acp] tool read Чтение — completed`), план агента,
  usage (`[acp] usage: used 1234 / size 200000`), мысли — усечённо и
  склеенно. Текст
  ответа идёт в stdout прогона: контракт результата, отчёты и авто-коммит
  не меняются. В TUI-хвосте виден живой прогресс.
- **Кэш-метрика своих агентов.** В протоколе ACP поля KV-кэша нет;
  расширение для своих харнессов — `_meta.cached_tokens` (или
  `_meta.prompt_cache_hit_tokens`) в `usage_update`: клиент покажет
  `, кэш N (NN% hit)` в строке usage (hit-rate — от `used`). Так видно,
  когда субагент ломает префиксный кэш провайдера (см. инцидент
  2026-09-23 с `claude-deepseek-proxy`).
- **Разрешения без дедлока.** Агент спрашивает
  `session/request_permission` и ждёт ответа; отвечает политика
  `acp_permission`: `readonly_auto` (дефолт) — читающие виды (`read`,
  `search`, `think`, `fetch`) разрешаются, остальное и неизвестное
  отклоняется; `deny` — всё отклонять; `allow_all` — всё разрешать.
  Запросы `fs/*`/`terminal/*` клиент не поддерживает (fail-closed:
  JSON-RPC `-32601` вместо зависания).
- **Те же таймауты и отмена:** абсолютный `timeout_secs`, тишина
  `idle_timeout_secs` (активность = любой ACP-кадр ИЛИ свежие mtime репо),
  прерывание — `session/cancel` + гашение процессной группы.

**Политика контекста — свежая сессия по умолчанию, продолжение только по
имени.** Без `acp_session` каждый прогон открывает новую сессию — это
осознанный дефолт: независимые проверки (ревью, повторные оценки) не
наследуют предвзятость предыдущего прогона. Для многошаговой работы задайте
алиас: `acp_session = "reviewer"`. Первый прогон откроет сессию и
запомнит её за алиасом (карта `state/acp-sessions.json`, ключ —
`харнесс/алиас`), следующие продолжат контекст через `session/resume`.
Семантика — «возобновить-или-создать»: недоступная сессия (перезапуск
агента, чистка) или агент без `sessionCapabilities.resume` — не ошибка
прогона, а свежая сессия с пометкой в логе. Выбор всегда виден строкой
`[acp] session: <id> (…)` в логе прогона: «свежая», «возобновлена, прогон
#N» или «новая вместо недоступной». TTL нет — смена алиаса = осознанный
сброс контекста.

Правило выбора: продолжать (алиас) — когда шаги работают в одном месте и
строятся друг на друге (разбор → правка → доработка); свежий (без алиаса) —
для независимых ревью и проверок, где наследуемый контекст вредит.

Дефолтные адаптеры (`Config::default` / `config.example.toml`; флаги
сверены с объявленным контрактом самих CLI — цитаты ниже; код 2 на argparse
ловит только грубую ошибку флага, но НЕ ловит «запустился интерактив вместо
headless», поэтому неинтерактивность подтверждается ещё и прогоном:
`scripts/adapters-headless-check.sh`):

| Харнесс | binary | args | prompt_mode | Объявленный неинтерактивный вызов (цитата справки CLI) |
|---|---|---|---|---|
| claude-code | `claude` | `["-p", "--dangerously-skip-permissions"]` | stdin | «use -p/--print for non-interactive output» |
| qwen-code | `qwen` | `["-p", "{prompt}"]` | flag | «Launch an interactive CLI, use -p/--prompt for non-interactive mode»; без флагов — интерактивная дефолтная подкоманда |
| openclaw | `openclaw` | `["agent", "--agent", "main", "--message", "{prompt}"]` | flag | `agent` — «Run an agent turn via the Gateway (use --local for embedded)»; на этой машине Gateway запущен (порт 18789), поэтому `--local` не нужен — на контуре без Gateway его надо добавить |
| hermes | `hermes` | `["-z", "{prompt}"]` | flag | «One-shot mode: send a single prompt and print ONLY the final response text to stdout» |
| theseus | `theseus` | `["-p", "{prompt}"]` | flag | «`-p, --prompt TEXT` — headless-режим без TUI» |
| codewhale | `codewhale` | `["exec", "--auto", "{prompt}"]` | flag | `exec` — «Run a non-interactive prompt», `--auto` — «Enable tool-backed agent mode with auto-approvals» |
| kimi-code | `kimi` | `["-p", "{prompt}"]` | flag | «Run one prompt non-interactively and print the response» |

Историческая заметка (2026-09-13): до этой сверки qwen-code и codewhale стояли
на формах, которые headless не давали — qwen на `[]` + stdin (запускалась
интерактивная дефолтная подкоманда), codewhale на верхнеуровневом `-p` без
описания (при том, что живые конфиги уже использовали `exec --auto`). Дрейф
«объявлено ≠ работает» копился в трёх местах сразу: встроенные дефолты,
`config.example.toml` и эта таблица.

Особенность kimi-code: permission-флаг не нужен и недопустим — в
`-p`-режиме regular-инструменты исполняются под auto-политикой (без
интерактивных промптов), а `--yolo`/`--auto` с `--prompt` несовместимы
(запуск отклоняется). Модель исполнителя — `-m <alias>` в `args`.

**Горячее перечитывание.** Инструмент `harness_run` перечитывает файл
конфига (тот путь, из которого он был загружен) при каждом вызове: правки
`[harnesses.*]` в ходе сессии применяются немедленно, перезапуск агента не
нужен. При сбое чтения используется снапшот, загруженный при старте.

**Обновление Spine и новые адаптеры.** Штатные адаптеры из `Config::default`
действуют только там, где нет файла конфигурации: существующий
`config.toml` полностью замещает таблицу `[harnesses]`, поэтому новый
адаптер из свежей версии Spine в ней автоматически НЕ появится — ошибка
«харнесс '…' не настроен» лечится копированием секции из
`config.example.toml` в ваш `config.toml` (благодаря горячему
перечитыванию — без перезапуска сессии).

**Коды выхода CLI** `arch-ml harness-run` (скриптовые гейты в пайпах):
`0` — прогон завершён, контракт `complete`/`partial` без конфликтов;
`1` — ошибка запуска/прерывание по таймауту; `2` — `status=blocked`;
`3` — непустой `conflicts_with_prior_decisions` (конфликт со spine
останавливает интеграцию по контракту).

**Скоуп fitness-правил.** `arch-ml control check` обходит репозиторий,
исключая служебные и производные каталоги (`.git`, `target`,
`node_modules`, `dist`, `__pycache__`, `.next`, `.pytest_cache` и
`.arch-handoff`): правила целятся в артефакты реализации, а не в документы
решения — иначе `must_not_contain` срабатывает на собственные цитаты
контракта из TASK.md/spine.

> **Claude Code headless:** `claude -p` без TTY на файловых операциях встаёт
> на permission-промпте и висит до таймаута — поэтому дефолтный адаптер несёт
> `--dangerously-skip-permissions`. Права процесса ограничены каталогом
> репозитория; для интерактивных прогонов флаг уберите в `config.toml`.

Прогон (`run_harness`): `cwd = repo`, env из конфига, захват stdout/stderr
(по 256 КБ хвоста каждый), код возврата, длительность и способ завершения
(`Termination`) в `HarnessRun`; бинарь не найден — ошибка с подсказкой
поправить `[harnesses.<name>]`.

**Умные таймауты.** Прогон прерывается по одному из двух триггеров:
абсолютный потолок `timeout_secs` или таймаут тишины `idle_timeout_secs`
(нет вывода в stdout/stderr И нет свежих изменений файлов репозитория —
процесс завис, например ждёт интерактивного ввода). Молчащий, но пишущий
код харнесс (heartbeat по mtime репо, скан ~4 раза за idle-окно) НЕ
считается зависшим. При прерывании убивается вся процессная группа
(TERM → 3 с → KILL по `-pgid`) — дочерние процессы харнесса не остаются
сиротами; частичный вывод и причина прерывания возвращаются в сводке с
рекомендацией проверить `git status` перед повторным запуском. Аргумент
`timeout_secs` инструмента `harness_run` ограничен снизу 600 с: модели
склонны занижать оценку, а ранний обрыв оставлял репозиторий полусобранным.
Агентный цикл применяет per-tool таймаут (`Tool::timeout_secs`, дефолт
300 с): у `harness_run` он покрывает самый длинный адаптер (до 7200 с +
запас) — раньше жёсткие 300 с обрывали прогон раньше таймаута адаптера.

Из агентного цикла (TUI/headless) прогон идёт инструментом **`harness_run`**
(не bash!): задача читается из `.arch-handoff/TASK.md` или передаётся явным
аргументом `task`; сводка ответа включает код возврата, длительность и
JSON-контракт результата (`status`, счётчики `assumptions`/`open_questions`/
`conflicts`) — отсутствие контракта помечается предупреждением. Bash-запуск
харнесса — антипаттерн: квотинг длинного TASK.md ломает команду, таймаут
bash (≤600 с) короче харнессового (1800 с), а env-scrub bash-инструмента
прячет `*_KEY`/`*_TOKEN` от команды — харнесс, авторизующийся через
переменную окружения, останется без креденшелов.

## Пример сессии end-to-end

```bash
arch-ml harnesses
#   claude-code    claude (Stdin)      /home/.../bin/claude
#   qwen-code      qwen (Stdin)        MISSING        ← бинаря нет: секция
#   ...                                             настраиваемая, прогон
#                                                   невозможен до установки
mkdir -p ~/work/payment-svc && cd ~/work/payment-svc && git init -q

arch-ml handoff hermes --repo ~/work/payment-svc \
  --task "Скелет платёжного оркестратора: команды списания/возврата, идемпотентность по payment_id+operation_seq" \
  --spec docs/ARCHITECTURE-SPINE.md
# Handoff-пакет: ~/work/payment-svc/.arch-handoff
#   ... TASK.md, ARCHITECTURE.md, MANIFEST.json, CONSTRAINTS.yaml, RUBRIC.yaml
# epic-context ≈ 900 токенов

arch-ml harness-run hermes --repo ~/work/payment-svc
# ── stdout (exit Some(0), 212.4s) ── ... {"status": "complete", ...}

arch-ml control check ~/work/payment-svc
# Правил: 4, нарушений: 1 (error: 0, warn: 1) ... Итог: PASS
```

Замечания:

- `qwen-code` в дефолтной конфигурации есть, но бинарь `qwen` на машине
  отсутствует (`MISSING` в `arch-ml harnesses`) — секция приведена как
  настраиваемый образец: установите Qwen Code или поправьте `binary`/`args`.
- Дефолтный `CONSTRAINTS.yaml` пакета зависит от стека репозитория (маркерные
  файлы): Rust (`cargo check`, `no-dbg-macro`…), Python (`pytest -q`,
  `no-print-in-py`…), Go (`go build`/`go vet`), Node (`npm test`), иначе —
  общий минимум (`readme-exists`). Это заготовка: перед передачей правила
  переписываются под spine-инварианты эпика (см. `assets/prompts/handoff_compile.md`);
  схема правил — в `docs/control.md`.
- Свой `CONSTRAINTS.yaml`/`RUBRIC.yaml` положите в `.arch-handoff/` до
  генерации или правьте после — повторная генерация их не затирает.
