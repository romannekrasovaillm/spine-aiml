# Контур диаграмм Archify (JSON IR → HTML/SVG)

Детерминистичный контур «архитектура как код»: агент (или человек) пишет
типизированный JSON IR, вендоренный Node.js CLI Archify компилирует его в
self-contained HTML/SVG с машинной приёмкой (9 artifact checks +
composition-профиль), SHA-256 receipt доставки и сравнением снапшотов
(Before/Delta/After). На этапе рендера LLM не участвует: та же входная
спецификация всегда даёт тот же артефакт, поэтому контур встраивается в
CI как гейт. Решение — ADR-027 (`docs/adr/`), механика — `src/archify.rs`,
методика авторинга — плагин `ru-archify` (скилл `archify-diagrams`,
банковская зона).

Не путать с `mermaid_render` (ADR-009): тот — черновой ASCII-арт для
проверки диаграммы в терминале, без презентационного качества, геометрии
и diff.

## Как устроена интеграция

Ядро — тонкая механика (AD-1), новых crate-зависимостей нет: `src/archify.rs`
запускает процесс `node <bin/archify.mjs> <args>` в рабочем каталоге с
таймаутом, окружение процесса проходит тот же scrub от секретов, что и у
bash-инструмента (`[bash] env_scrub`, AD-3). JSON-receipt CLI разбирается в
компактную сводку для модели/терминала; провал CLI (ненулевой exit, таймаут)
— это разобранные диагностики с `supportedFixes` (петля ремонта), а не
падение хода. Знания (как писать IR) — в плагине `ru-archify` (AD-10).

## Пин версии движка

Совместимость харнесса с CLI держится на контракте receipt
(`schemaVersion: 1`), проверенном с вендоренной копией `vendor/archify/`.
Проверенная версия зашита константой `PINNED_CLI_VERSION` в
`src/archify.rs`; `arch-ml doctor` читает версию установленного CLI
(`skill-release.json`, затем `package.json` рядом с `bin/`) и
предупреждает о расхождении (Warn, не блокирует). Установка из сети
(`npx skills add tt-a1i/archify`) тянет непроверенный latest — это
fallback, а не каноничный путь; каноничный — вендоренный tarball релиза
дистрибутива (`archify-vendored-*.tar.gz`) или `vendor/archify/` здесь.
Обновление `vendor/archify/` = bump `PINNED_CLI_VERSION` + повторная волна
живой проверки дистрибутива.

## Пять типов IR

Допустимые значения `type` (константа `DIAGRAM_TYPES` в `src/archify.rs`,
совпадает с `--help` CLI): `architecture`, `workflow`, `sequence`,
`dataflow`, `lifecycle`. Назначение — по методике вендоренного пакета
(`vendor/archify/SKILL.md`):

| Тип | Назначение |
|---|---|
| `architecture` | Компоненты, сервисы, границы контуров (cloud/security), инфраструктура. Единственный тип, поддерживаемый `compare`. |
| `workflow` | Процессы, гейты согласования, вызовы инструментов, runbook'и, CI/CD. |
| `sequence` | Цепочки вызовов API, жизненный цикл запроса, async-трассы, возвраты. |
| `dataflow` | Пайплайны, ETL/ELT, lineage, говернанс, потребители данных. |
| `lifecycle` | Конечные автоматы: переходы состояний/статусов, ретраи, ожидание, терминальные состояния. |

Файл IR именуется `<имя>.<тип>.json` (напр. `sbp-v1.architecture.json`) и
версионируется в git — это исходник диаграммы. Если тип неочевиден,
рекомендацию даёт `arch-ml archify guide "<сценарий>"`.

## Конфигурация `[archify]`

Секция в `~/.config/arch-ml/config.toml` (личные пути — только здесь,
никогда в коде; поля — `ArchifyConfig` в `src/config.rs`):

```toml
[archify]
enabled = true                      # false — инструменты archify_* не регистрируются в сессии
cli_path = "<репо>/vendor/archify/bin/archify.mjs"
node_bin = "node"                   # Node.js >=18
timeout_secs = 120                  # потолок одного вызова CLI (compare — до 900)
```

| Поле | Дефолт | Семантика |
|---|---|---|
| `enabled` | `true` | `false` снимает регистрацию инструментов `archify_*` (гейт на уровне регистрации, как `[web].enabled`). |
| `cli_path` | пусто | Путь к `bin/archify.mjs`. Пусто — интеграция не настроена: инструменты и CLI отвечают инструкцией по установке. |
| `node_bin` | `node` | Исполняемый файл Node.js (≥18). |
| `timeout_secs` | `120` | Таймаут одного вызова CLI, сек; значение прижимается к диапазону 1–900 (`MAX_TIMEOUT_SECS`). |

Проверка настройки: строка `archify` в `arch-ml doctor` и отдельно
`arch-ml archify doctor` (см. ниже).

## CLI `arch-ml archify`

Подкоманды — прокси к Archify CLI (`src/main.rs`, `cmd_archify`): вывод
печатается в stdout, код возврата становится гейтом для CI/скриптов.
Синтаксис ниже — из живых прогонов `--help`.

```text
arch-ml archify doctor
arch-ml archify guide <QUERY>
arch-ml archify validate <TYPE> <PATH> [--quality standard|showcase] [--json]
arch-ml archify deliver  <TYPE> <PATH> <OUTPUT> [--quality standard|showcase] [--json]
arch-ml archify compare  <BASE> <HEAD> <OUTPUT> [--quality standard|showcase] [--json]
```

| Подкоманда | Назначение |
|---|---|
| `doctor` | Проверка окружения: версия node, шаблон, рантаймы, валидаторы схем, рендеры всех пяти типов. |
| `guide` | Рекомендация типа диаграммы и сценария под запрос на естественном языке (JSON: `recommendation` с `confidence`, `useWhen`/`avoidWhen`, `include`, готовый `prompt`). |
| `validate` | Валидация IR: 9 artifact checks + composition-профиль. |
| `deliver` | Финальная приёмка: валидация + атомарная запись self-contained HTML + SHA-256 receipt спецификации и артефакта. |
| `compare` | Дельта двух `architecture`-снапшотов (base → head): страница Before/Delta/After + машинный receipt. |

### Вывод и флаг `--json`

- Без `--json` `validate`/`deliver`/`compare` печатают компактную сводку
  receipt (та же, что получает модель в TUI); `doctor` и `guide` — сырой
  вывод CLI.
- С `--json` в stdout идёт сырой pretty-printed JSON-receipt Archify CLI
  (`schemaVersion: 1`) — это SDK-контракт v1, см. `sdk/CONTRACT.md` §3
  (общие поля `ok`, `command`, `type`; профильные — `checks`,
  `composition`, `specification`/`artifact`, `summary`, `changes`).
- При провале JSON-receipt с `"ok": false` и массивом `diagnostics` всё
  равно печатается в stdout — SDK различает «FAIL по правилам» и ошибку
  исполнения.

### Коды выхода (живые прогоны + код `cmd_archify`)

| Ситуация | Код `arch-ml` | Что в выводе |
|---|---|---|
| `ok: true` | 0 | сводка / JSON-receipt |
| Провал валидации (CLI exit 1) | 1 | receipt с `ok:false` + `Error: archify <cmd>: провал (код выхода 1)` в stderr |
| Ошибка использования CLI — неизвестный тип/профиль (CLI exit 2) | 1 | `Unknown diagram type "…"` / `Unknown quality profile "…"` + `Error: … провал (код выхода 2)` в stderr |
| Таймаут | 1 | `Error: archify <cmd>: таймаут <N> сек` |

Т.е. для гейтов: `0` — приёмка пройдена, любой провал — `1`; исходный код
Archify CLI виден в сообщении stderr.

## Quality-профили: standard vs showcase

Флаг `--quality` (дефолт `showcase` — и в CLI, и в агентных инструментах;
в самом IR задаётся `meta.quality_profile`). Профиль управляет строгостью
composition-гейтов (`renderers/shared/geometry.mjs` вендоренного пакета):

- **Инварианты корректности работают в любом профиле**: схема IR, ребро
  сквозь посторонний узел (`clean-flow/edge-through-node`), конечность
  геометрии — это ошибки и в standard.
- **showcase** дополнительно включает строгие визуальные бюджеты как
  ошибки: собственные X-пересечения несвязанных маршрутов
  (`relationship_crossings`), слияние коридоров ≥8 px
  (`composition/ambiguous-corridor`), зазор метки до чужого маршрута <4 px
  (`composition/label-route-clearance`), ритм маршрута — внутренние
  сегменты <16 px и микросегменты <8 px (`route_rhythm`).
- В **standard** те же находки приходят предупреждениями и не ломают
  приёмку.

Живой пример на одном и том же IR (два маршрута с общим коридором и
наложенными метками, `pos` заданы вручную):

```text
$ arch-ml archify validate architecture cross.json --quality standard
archify validate: ok
checks: 9/9
composition: pass (errors 0, warnings 3)          # EXIT=0

$ arch-ml archify validate architecture cross.json --quality showcase
archify validate: ПРОВАЛ
error: Architecture layout validation failed: | - [composition/ambiguous-corridor] … | - [composition/label-route-clearance] …
diagnostics (3):
- [error] composition/ambiguous-corridor: … shares a 260px corridor …
  fix: adjust route/via or fromSide/toSide so unrelated connections do not visually merge
- [error] composition/label-route-clearance: … is 0px from … (minimum 4px) …
…
Error: archify validate: провал (код выхода 1)     # EXIT=1
```

Практика из скилла `archify-diagrams`: приёмка для комитета — showcase,
9/9 checks, composition pass, 0 ошибок и 0 предупреждений; standard — для
плотных рабочих карт, где строгая геометрия не нужна.

## Типовой workflow

Обязательный порядок (из скилла `ru-archify`): **авторинг IR → validate →
deliver → compare**. Прогоны ниже выполнены на эталонных фикстурах
`banking/demos/cli-from-claude-code/scenario2-archify-cli/`
(`sbp-v1.architecture.json`, `sbp-v2.architecture.json` — снапшоты
СБП-контура; те же файлы — фикстуры SDK-тестов по `sdk/CONTRACT.md` §5).

### 1. Авторинг IR

Агент пишет IR по скиллу `ru-archify` (`archify-diagrams`): один главный
путь, не более ~12 первичных узлов, стабильные `id` у компонентов и связей
(id — контракт идентичности для compare: переименование id =
removed+added), типы компонентов строго из enum (`frontend`, `backend`,
`database`, `cloud`, `security`, `messagebus`, `external`), геометрия
вручную (`via`/`fromSide`/`labelAt`) — только по диагностике валидатора.
Образец формы полей —
`banking/plugins/ru-archify/skills/archify-diagrams/references/bank-target-landscape.architecture.json`.

### 2. validate — приёмка после каждой правки

```text
$ arch-ml archify validate architecture sbp-v1.architecture.json
archify validate: ok
checks: 9/9
composition: pass (errors 0, warnings 0)
```

Девять artifact checks (имена — из JSON-receipt): `single_svg`,
`finite_svg`, `orthogonal_arrows`, `label_route_clearance`,
`relationship_crossings`, `relationship_corridors`,
`container_border_runs`, `route_rhythm`, `legend_clearance`. При провале —
читать `diagnostics`: `code`, `severity`, измеренное `message`, готовые
`supportedFixes`; чинить точечно и повторять.

### 3. deliver — заморозка артефакта

```text
$ arch-ml archify deliver architecture sbp-v1.architecture.json sbp-v1.html
archify deliver: ok
validation: 9/9 checks, errors 0, warnings 0
spec: sha256 cb492b86486d8f9c2f3db3e5003ce0e09d4f557da97560a26868e3622d26a20b (7456 байт)
artifact: sha256 4ac963b6b923e88c68a5d0c78db389800a0a345706e20148943e860cd68779e1 (725537 байт)
```

HTML — self-contained (офлайн, без телеметрии), запись атомарная.
После успешной доставки IR заморожен: новая правка = новый цикл
validate → deliver, иначе receipt теряет смысл.

### 4. compare — фактологический diff для комитета

```text
$ arch-ml archify compare sbp-v1.architecture.json sbp-v2.architecture.json sbp-delta.html
archify compare: ok
validation: 28/28 checks, errors 0, warnings 0
artifact: sha256 753d12624e4bf7d1b220f726915688deb214ae07213d51c6f4fd76ac8b9fbb28 (2077581 байт)
delta: {"boundaries":{"added":1,"changed":1,"geometryChanged":0,"removed":0},
        "components":{"added":1,"changed":0,"evidenceChanged":0,"moved":0,"removed":0},
        "connections":{"added":2,"changed":0,"removed":0,"rerouted":1},
        "presentationChanged":true,"provenanceChanged":false}
```

Из `--json`-receipt того же прогона: `completeness: "complete"`,
`proofLevel: "authored"`, у `base`/`head` — `title`, `rawSha256` и
`semanticSha256` каждого снапшота, детализация в `changes`
(здесь: 1 компонент, 3 связи, 2 границы). Помимо stdout, `compare` пишет
sidecar-файл `<output-base>.receipt.json` рядом с delta HTML (в фикстурах
сценария — `sbp-delta.receipt.json`) — полный машинный receipt для
протокола комитета. Результат — страница Before/Delta/After (delta HTML)
плюс машинный receipt: точные факты added/removed/changed/moved/rerouted
по стабильным id. Compare сознательно **не выводит**
риск/импакт/mergeability — оценка остаётся за архитектором.

## Агентные инструменты в TUI

Те же три операции доступны модели как инструменты (полные параметры —
`docs/tools.md`, раздел «Диаграммы»):

| Инструмент | Обязательные параметры | Особенности |
|---|---|---|
| `archify_validate` | `type`, `path` | Провал — `ToolOutput::err` с диагностиками и `supportedFixes` (петля ремонта модели). |
| `archify_deliver` | `type`, `path`, `output` | `output` fail-closed внутри рабочего каталога (путь за пределами cwd отклоняется до запуска CLI). |
| `archify_compare` | `base`, `head`, `output` | Только `architecture`; тот же receipt, что у CLI. |

Общий опциональный параметр — `quality` (по умолчанию `showcase`).
Инструменты регистрируются только при `[archify].enabled = true` и
настроенном `cli_path`; иначе отвечают инструкцией по установке (это
подсказка модели, не сбой вызова).

## Вендоринг `vendor/archify`

Archify (github.com/tt-a1i/archify, MIT) вендорен в монорепозиторий —
продукт самодостаточен: клон + Node ≥18 = работающий контур без
`npx skills add` и внешних скачиваний (ИБ-контур банка). Политика —
`vendor/README.md`:

- Текущая версия: **2.17.0-dev.1** (вендорена 2026-09-03; см.
  `vendor/archify/package.json` → `version`).
- Рантайм zero-dependency (схемные валидаторы прекомпилированы в
  standalone ESM); `node_modules` не вендорится и нужен только для
  перегенерации валидаторов и прогона upstream-тестов при обновлении.
- Код не модифицируется точечно — только целостные обновления версии:
  rsync свежего клона (без `node_modules`/`.git`) → гейты
  (`node vendor/archify/bin/archify.mjs doctor`, validate образца из
  `ru-archify`, `cargo test archify`, `arch-ml control check .`) → коммит
  с тегом версии. Лицензии (`LICENSE`, `THIRD_PARTY_NOTICES.md`)
  сохраняются.
- Требование окружения: Node.js ≥18 на машине (`node_bin`). Проверено на
  Node v22: `arch-ml archify doctor` — 15 строк `[ok]`, финал
  `Archify is ready.`

## Ограничения и типовые ошибки

Ограничения:

- Потолок одной диаграммы — **~12 первичных узлов** (ограничение Archify,
  ADR-027): ландшафт дробится на виды по 42010 (связка со скиллом
  `viewpoint-concern-mapping`).
- `compare` работает только для `architecture`-снапшотов.
- Хром вьюера (кнопки) — английский (локали CLI: en, zh-CN); русские
  подписи в контенте — норма, читателя предупредить.
- Зависимость от внешнего Node.js ≥18: при его отсутствии интеграция
  деградирует мягко (Warn в `arch-ml doctor`, инструкция по установке в
  ответе инструментов), ядро без неё полностью работоспособно.

Типовые ошибки (все, кроме двух последних строк, воспроизведены живьём
через `arch-ml archify validate`; таймаут и ненастроенный CLI — по коду и
unit-тестам `src/archify.rs`):

| Ошибка | Диагностика в receipt | Ремонт |
|---|---|---|
| Неизвестный тип (`graph`) | `Unknown diagram type "graph". Expected one of: architecture, workflow, sequence, dataflow, lifecycle` (CLI exit 2) | Исправить `type`. |
| Неизвестный профиль (`gold`) | `Unknown quality profile "gold". Expected standard or showcase.` (CLI exit 2) | `--quality standard\|showcase`. |
| Файл не найден | `[error] input/read: Input could not be read: ENOENT …`; fix: `provide one readable JSON input file` | Проверить путь (резолвится от cwd). |
| Нет обязательных полей схемы | `[error] schema/required: /components/0 … must have required property 'type'`; fix: `add required property "type"` | Дописать поля; лишние — `schema/additionalProperties` (`must NOT have additional properties {"additionalProperty":"name"}`). |
| Недопустимое значение enum | `[error] schema/enum: … must be equal to one of the allowed values ["frontend","backend","database","cloud","security","messagebus","external"]` | Тип компонента строго из enum. |
| `meta.locale: "ru"` | `[error] schema/enum: /meta/locale … must be equal to one of the allowed values ["en","zh-CN"]` — ломает валидацию даже при полностью русском контенте | `meta.locale` — только `en`/`zh-CN` (или опустить); на язык подписей не влияет (догфуд-находка 2026-09-03). |
| Свободное размещение без координат | `[error] layout/constraint: Component "a" must include pos [x, y] when layout.mode is omitted (free placement).` | Задать `pos`/`size` или режим layout. |
| Связь в несуществующий узел | `[error] layout/constraint: Connection "вызов" references unknown target "nonexistent".` | Починить `from`/`to` (опечатка в id). |
| Composition-нарушения в showcase | `[error] composition/ambiguous-corridor`, `composition/label-route-clearance` и др. с координатами сегмента и `fix:` | Точечно `route`/`via`/`fromSide`/`labelAt` — не более одного геометрического контроля за ремонт. |
| Таймаут | `archify <cmd>: таймаут <N> сек` | Упростить диаграмму или поднять `[archify].timeout_secs` (потолок 900). |
| CLI не настроен | `Archify не настроен: укажите … секцию [archify] с cli_path = …` | Прописать `cli_path` на `vendor/archify/bin/archify.mjs`. |

Если два раунда ремонта не уменьшают число ошибок — остановиться и честно
доложить нерешённые диагностики (контракт Archify из скилла
`archify-diagrams`).
