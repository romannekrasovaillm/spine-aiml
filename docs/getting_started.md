# Начало работы

Сквозной гайд для исследователя или разработчика, впервые запускающего
Spine AI/ML Edition: от сборки бинаря `arch-ml` до первого headless-прогона,
архитектурного гейта, диаграммы и вызова из SDK. Все команды проверены на
живой установке; пути в репозитории обозначены `<репо>`.

Полный обзор возможностей — `README.md` и `docs/features.md`; устройство
харнесса — `docs/architecture.md`.

## 1. Требования

| Компонент | Минимум | Зачем |
|---|---|---|
| Rust (stable) | 1.85 (MSRV, `rust-version` в `Cargo.toml`) | сборка `arch-ml` |
| Node.js | ≥ 18 | контур диаграмм Archify (`vendor/archify`, zero-dependency рантайм) |
| Python | ≥ 3.10 (опционально) | `sdk/python` — только стандартная библиотека |
| JDK | 21 (опционально) | `sdk/java` — ноль внешних зависимостей |

Для LLM-вызовов нужен ключ хотя бы одного OpenAI-совместимого провайдера
(`DEEPSEEK_API_KEY`, `ZHIPU_API_KEY`, `KIMI_API_KEY`, …). Без ключа работают
все детерминированные команды: `doctor`, `control`, `archify`, `mermaid`,
гейты и SDK поверх них.

## 2. Сборка

```bash
cd <репо>
cargo build --release          # бинарь: target/release/arch-ml
ln -sf "$PWD/target/release/arch-ml" ~/.local/bin/arch-ml   # запуск одним словом
```

Дальше в тексте — `arch-ml`; если симлинк не создавали, подставляйте
`<репо>/target/release/arch-ml`.

## 3. Конфигурация

Порядок поиска конфига при запуске (`src/config.rs::load`):

1. `--config <path>` (есть у каждой команды);
2. `./arch-ml.toml` (текущий каталог);
3. `~/.config/arch-ml/config.toml`;
4. встроенные дефолты (полностью задокументированы в `config.example.toml`).

```bash
arch-ml init    # конфиг + ассеты (промпты, рубрики, плагины, примеры) в
                # ~/.arch-ml и ~/.config/arch-ml/config.toml
```

Все секции конфига опциональны — указывайте только отличия от дефолтов.
Минимальная рабочая секция — одна модель:

```toml
default_model = "deepseek"

[models.deepseek]
base_url = "https://api.deepseek.com/v1"   # любой OpenAI-совместимый endpoint
model = "deepseek-flash"
api_key_env = "DEEPSEEK_API_KEY"           # ИМЯ переменной, не значение
max_tokens = 8192
timeout_secs = 180
context_limit = 1000000
```

Правила:

- **Значения ключей в конфиг не пишутся никогда** — только `api_key_env`
  (имя env-переменной) или `api_key_file` (путь к файлу с ключом). Ключ
  резолвится лениво при первом вызове провайдера и уходит как `Bearer`.
- Личные пути (база знаний, библиотеки плагинов) живут только в
  `~/.config/arch-ml/config.toml`, не в репозитории.
- Для Archify пропишите вендоренный движок:
  `cli_path = "<репо>/vendor/archify/bin/archify.mjs"` в секции `[archify]`
  (`node_modules` не нужен — см. `vendor/README.md`).

```bash
export DEEPSEEK_API_KEY=<ваш ключ>   # в ~/.bashrc или окружении сессии
```

## 4. Проверка окружения

```bash
arch-ml doctor           # ключи, каталоги, плагины, харнессы, MCP
arch-ml archify doctor   # node, CLI Archify, рендеры пяти типов диаграмм
```

Реальный вывод (цифры на вашей машине будут свои):

```
$ arch-ml doctor
arch-ml doctor — диагностика окружения

  ✓ default_model  «deepseek» → deepseek-flash
  ✓ api-keys       все 18 ключей на месте
  ✓ sessions       ~/.arch-ml/sessions — запись возможна
  ✓ plugins        11 плагинов, 74 скиллов
  ✓ knowledge      3 из 3 каталогов доступны
  ✓ harnesses      в PATH: 10 (claude-code, codewhale, hermes, kimi-code, logtest, openclaw, qwen-code, theseus, theseus-max, theseus-yolo); отсутствуют: —
  ✓ mcp            4 серверов в ~/.arch-ml/mcp.json
  ✓ cron           ~/.arch-ml/cron.toml на месте
  ✓ web            11 кураторских сайтов архитектурных знаний
  ✓ archify        node 'node' + CLI <репо>/vendor/archify/bin/archify.mjs
  ✓ git            в PATH

Итог: здоров (11 проверок)
```

```
$ arch-ml archify doctor
Archify doctor

[ok] Node.js v22.23.1 (requires >=18)
[ok] Core template
[ok] Example renderer
[ok] Live preview runtime
[ok] Visual-check runtime
[ok] Output path safety runtime
[ok] Scenario recipe guide
[ok] Progressive authoring references
[ok] Architecture compare runtime and proof fixtures
[ok] Standalone schema validators
[ok] architecture renderer, schema, and example
[ok] workflow renderer, schema, and example
[ok] sequence renderer, schema, and example
[ok] dataflow renderer, schema, and example
[ok] lifecycle renderer, schema, and example

Archify is ready.
```

Красная проверка `doctor` — не приговор: без LLM-ключей и харнессов
работают контроль, диаграммы, рубрики и SDK; сообщение скажет, чего
не хватает.

## 5. Первый интерактив: TUI

```bash
arch-ml          # интерактивный TUI — команда по умолчанию
```

Диалог с моделью в терминале (ratatui, Tokyo Night): строка ввода,
стриминг ответа, панели инструментов. Управление — слэш-команды:
`/help` (справка), `/model` (пикер модели), `/think on|off|auto`
(ризонинг-режим), `/new` (новая сессия), `/quit` (выход). Полный
справочник — `docs/slash_commands.md`.

## 6. Первый headless-прогон

Строгий headless-контракт `run -q`: stdout — только финальный ответ
ассистента, прогресс молчит; при успехе stderr пуст, при сбое — причина
в stderr и exit 1. `--timeout` — общий потолок прогона (страховка CI/cron
от зависшего провайдера).

```bash
arch-ml run -q --model glm-5.3-flash --timeout 240 --max-turns 1 \
  "Ты — ML-архитектор. Перечисли 5 обязательных компонентов контура \
воспроизводимого обучения маленькой модели и по одному ключевому NFR на \
каждый. Формат: нумерованный список, каждая строка: компонент — NFR. \
Без вступлений." \
  > answer.md
```

Ответ придёт одним списком в `answer.md` (exit 0). Зафиксированные живые
прогоны с выводом — в кейсах `кейсы/` (например, evidence-файлы
`laguna-compact` и `kimi-killer`).

Промпт можно подать и через stdin: `cat spec.md | arch-ml run -`.
Полезные флаги: `--no-stream` (только финальный ответ),
`--think on|off` (карта ризонинга из конфига модели).

## 7. Первый архитектурный гейт

`control check` — детерминированный fitness-контроль репозитория по
`CONSTRAINTS.yaml`, без LLM. Итог PASS/FAIL; при FAIL — **exit 1**
(годится для CI). Учебный набор правил из пресета AI/ML-исследователя
(ML-06 «воспроизводимость прогона», ML-09 «бюджет прогона») —
`examples/ml-experiment/`; пример нарочито красный:

```bash
cd examples/ml-experiment
arch-ml control check . --constraints CONSTRAINTS.yaml
```

```
Правил: 3, нарушений: 3 (error: 3, warn: 0)
  [error] run-manifest.yaml:0 budget_declared — must_contain: паттерн 'budget' не найден ни в одном файле по glob 'run-manifest.yaml'
  [error] run-manifest.yaml:0 run_manifest_present — file_exists: файл не найден: run-manifest.yaml
  [error] train*.py:0 seed_declared — must_contain: паттерн '(?i)seed\s*=' не найден ни в одном файле по glob 'train*.py'
Итог: FAIL
# exit 1
```

Находка указывает файл, правило и чего не хватает. Исправим по README
примера (seed в скрипте + манифест прогона с бюджетом):

```bash
cp fix/train.py train.py && cp fix/run-manifest.yaml run-manifest.yaml
arch-ml control check . --constraints CONSTRAINTS.yaml
```

```
Правил: 3, нарушений: 0 (error: 0, warn: 0)
Итог: PASS
# exit 0
```

Схема правил и остальные типы проверок — `docs/control.md`; `--json` —
машиночитаемый отчёт `FitnessReport` (SDK-контракт v1).

## 8. Первая диаграмма

Archify — контур «архитектура как код»: JSON IR → валидация (9 artifact
checks + composition-профиль) → атомарная доставка HTML с SHA-256
receipt. Фикстура — ML-пайплайн обучения —
`examples/archify/ml-pipeline-v1.architecture.json`:

```
$ arch-ml archify validate architecture examples/archify/ml-pipeline-v1.architecture.json
archify validate: ok
checks: 9/9
composition: pass (errors 0, warnings 0)
# exit 0

$ arch-ml archify deliver architecture examples/archify/ml-pipeline-v1.architecture.json /tmp/ml-pipeline-v1.html
archify deliver: ok
validation: 9/9 checks, errors 0, warnings 0
spec: sha256 5bd7f3574b4bbbb0ea97c4fe7835fbd61c1d83f8254723bafd6d95542fc24530 (8009 байт)
artifact: sha256 c8be429bce99944e16eb91edce264245813b7b32bb79717eb086c6df30e4ea72 (726120 байт)
# exit 0
```

`deliver` атомарен: HTML либо доставлен целиком с receipt, либо не
появился вовсе. Дельта двух версий спецификации — `archify compare`
(машинный diff added/removed/changed/rerouted + HTML Before/Delta/After).

## 9. Первый вызов из SDK

SDK — тонкие клиенты поверх headless CLI (без shell, без сети; контракт
v1 — `sdk/CONTRACT.md`). Бинарь разрешается так: параметр `binary` →
`SPINE_BE_BIN` → `arch-ml` из `PATH` (шаг 2 уже позаботился). Готовый
пример — CI-гейт на Python SDK поверх учебного ML-проекта:

```bash
python3 sdk/python/examples/ci_gate.py examples/ml-experiment \
  --constraints examples/ml-experiment/CONSTRAINTS.yaml
```

```
Репозиторий: examples/ml-experiment
Сводка: Правил: 3, нарушений: 3 (error: 3, warn: 0)
Нарушения:
  run-manifest.yaml [error] budget_declared — must_contain: паттерн 'budget' не найден ...
  run-manifest.yaml [error] run_manifest_present — file_exists: файл не найден: run-manifest.yaml
  train*.py [error] seed_declared — must_contain: паттерн '(?i)seed\s*=' не найден ...
ГЕЙТ: FAIL
# exit 1
```

Коды выхода примера: 0 — гейт зелёный, 1 — гейт красный (нарушения —
это данные, не ошибка), 2 — ошибка исполнения (бинарь не найден, процесс
упал). Красный отчёт приходит типизированным `FitnessReport` с находками
«файл:строка — правило — сообщение»; `passed=false` — валидные данные,
не исключение. После исправления примера (шаг 7) тот же вызов даёт
`ГЕЙТ: PASS` и exit 0. То же API — на Rust и Java (`sdk/README.md`).

## 10. Куда дальше

| Раздел | Ссылка |
|---|---|
| Портал документации | `docs/README.md` |
| Полный обзор возможностей | `docs/features.md` |
| Слэш-команды TUI | `docs/slash_commands.md` |
| Архитектурный контроль: триггеры, spine, fitness, гейты | `docs/control.md` |
| Справочник инструментов агента | `docs/tools.md` |
| Устройство харнесса | `docs/architecture.md` |
| Модели и провайдеры | `docs/models.md` |
| SDK: контракт и клиенты (Python/Rust/Java) | `sdk/CONTRACT.md`, `sdk/README.md` |
| Примеры в репозитории | `examples/`: `ml-experiment` (гейт ML-06/ML-09), `archify/` (JSON IR), `mermaid/`, `specs/`, `corp-spine/` |
| Доменные кейсы AI/ML | `кейсы/laguna-compact/`, `кейсы/kimi-killer/` |
