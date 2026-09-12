# ADR-027. Интеграция Archify — детерминистичный контур диаграмм «архитектура как код» (JSON IR → HTML/SVG) в ядро и банковскую зону

- Date: 2026-09-03
- Status: Accepted

## Context

Диаграммы в банковских спеках рисуются вручную: не версионируются, не
проверяются, расходятся с кодом; «было/стало» на архитектурный комитет
приносят двумя картинками без машинного diff. В ядре есть только
`mermaid_render` — черновой ASCII-арт для проверки в терминале (ADR-009),
без презентационного качества, валидации геометрии и сравнения снапшотов.

Archify (github.com/tt-a1i/archify, MIT) — Node.js CLI + agent skill:
типизированный JSON IR (architecture/workflow/sequence/dataflow/lifecycle)
детерминистично компилируется в self-contained HTML/SVG с 9 artifact
checks + composition-профилем, `deliver` с SHA-256 receipt и `compare`
(Before/Delta/After с машинным receipt added/removed/changed/moved/
rerouted по стабильным id). Zero-dependency рантайм, без телеметрии —
проходит ИБ-контур банка. Продукт предварительно исследован живьём
(установка, 6 кейсов, тест-сьют 1026 тестов / 0 провалов) —
отчёт `Archify_живое_исследование_продукта_2026-09-03.docx`.

Significance: 1 триггер (new_component — новый доменный модуль ядра
и плагин банковской зоны) → маршрут Standard; решение владельца
получено прямым указанием («глубокая интеграция Archify в Spine BE»).

## Decision

1. **Ядро — тонкая механика (AD-1), без новых crate-зависимостей**
   (fitness no-new-dependencies): модуль `src/archify.rs` — запуск
   `node <cli>` с таймаутом, scrub окружения (разделяет `scrub_env`
   с bash, AD-3), разбор JSON-receipt в компактную сводку с
   диагностиками (`code`/`evidence`/`supportedFixes`) для петли
   ремонта модели. Три инструмента: `archify_validate`,
   `archify_deliver`, `archify_compare`; провал CLI — `ToolOutput::err`,
   не падение хода. Выходные пути fail-closed внутри рабочего каталога.
2. **Конфиг**: секция `[archify]` (`enabled`, `cli_path`, `node_bin`,
   `timeout_secs`) — личные пути только в пользовательском конфиге;
   `enabled = false` снимает регистрацию инструментов (гейт на уровне
   регистрации, как `[web].enabled`, AD-4).
3. **CLI**: `arch-be archify doctor|guide|validate|deliver|compare` —
   headless-гейт для CI (exit code CLI = exit code `arch-be`).
4. **Doctor**: чек `archify` (node в PATH + настроенный `cli_path`;
   ненастроенная интеграция — Warn, не Fail).
5. **Знания — в плагинах (AD-10)**: банковский плагин `ru-archify`
   (proprietary, ADR-018) со скиллом `archify-diagrams`: маршрутизатор
   типов на банковские артефакты, обязательный цикл
   авторинг → validate (showcase 9/9) → deliver → compare для комитета,
   антипаттерны, образец валидного IR банковского контура
   (`references/bank-target-landscape.architecture.json`).

## Consequences

- Архитектор получает версионируемые, машинно-проверяемые диаграммы и
  фактологический diff снапшотов для комитета; compare честно не выводит
  риск/импакт — оценка остаётся за архитектором.
- Зависимость от внешнего Node.js ≥18 и локально установленного Archify:
  интеграция деградирует мягко (инструкция по установке в ответе
  инструмента, Warn в doctor), ядро без неё полностью работоспособно.
- Потолок одной диаграммы ~12 узлов (ограничение Archify): ландшафт
  дробится на виды по 42010 (связка со скиллом
  `viewpoint-concern-mapping`).
- Граница лицензий сохранена: Archify (MIT) вызывается как внешний
  процесс; методика в `banking/` — proprietary (BE-01).
