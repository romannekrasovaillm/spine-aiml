# Начало работы

Сквозной гайд для архитектора или разработчика, впервые запускающего
Spine-BE: от сборки бинаря `arch-ml` до первого headless-прогона,
архитектурного гейта, диаграммы и вызова из SDK. Все команды проверены на
живой установке; пути в репозитории обозначены `<репо>`.

Полный обзор возможностей — `README.md`; устройство харнесса —
`docs/architecture.md`.

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
  ✓ archify        node 'node' + CLI ~/spine-bank/vendor/archify/bin/archify.mjs
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
  "Ты — solution-архитектор банка. Перечисли 5 обязательных компонентов \
контура для P2P-переводов по СБП и по одному ключевому NFR на каждый. \
Формат: нумерованный список, каждая строка: компонент — NFR. Без вступлений." \
  > answer.md
```

Пример вывода — из зафиксированного живого прогона
(`banking/demos/cli-from-claude-code/scenario1-headless/test-run.md`,
exit 0 за 2 мин 39 с):

```
1. Канал инициирования (мобильный/интернет-банк, аутентификация и подтверждение операции клиентом) — доступность ≥ 99,9% в месяц на функцию инициирования перевода.
2. Шлюз СБП-НСПК (API проверки получателя и перевода, статусная модель, ГОСТ TLS/подписи) — латентность синхронной фазы проведения p99 ≤ 3 с [значение сверить с регламентом НСПК — ТРЕБУЕТ ПРОВЕРКИ].
3. Процессинг счетов (списание/зачисление в ядре, идемпотентность потребителя, transactional outbox) — строгая согласованность проводок: RPO = 0, нулевой допуск расхождений бухгалтерских позиций.
4. Антифрод и комплаенс real-time (скоринг, лимиты СБП, STOP-list, 115-ФЗ) — латентность решения скоринга p99 ≤ 200 мс внутри общего бюджета транзакции.
5. Журнал операций и сверка (неизменяемый журнал, приём статусов, recon с НСПК) — полнота 100%: ни одна операция не теряется; расхождения сверки детектируются ≤ 24 ч (D+1).
```

Промпт можно подать и через stdin: `cat spec.md | arch-ml run -`.
Полезные флаги: `--no-stream` (только финальный ответ),
`--think on|off` (карта ризонинга из конфига модели).

## 7. Первый архитектурный гейт

`control check` — детерминированный fitness-контроль репозитория по
`CONSTRAINTS.yaml`, без LLM. Итог PASS/FAIL; при FAIL — **exit 1**
(годится для CI). Учебный набор правил «банковский контур СБП» —
`banking/demos/cli-from-claude-code/scenario3-gate/fixtures`
(BANK-01: нет PAN в коде; BANK-02: `Idempotency-Key`; BANK-03: шапка
лицензии). Прогоняем на копии:

```bash
cp -r banking/demos/cli-from-claude-code/scenario3-gate/fixtures /tmp/arch-gate
cd /tmp/arch-gate
```

Зелёный прогон:

```
$ arch-ml control check . --constraints CONSTRAINTS.yaml
Правил: 3, нарушений: 0 (error: 0, warn: 0)
Итог: PASS
# exit 0
```

Красный прогон — добавим файл с тестовым PAN:

```bash
cat > src/hotfix.py <<'EOF'
# быстрый хотфикс: тестовый PAN зашит в код
pan = "4276550012345678"  # TODO: убрать тестовый PAN
EOF
arch-ml control check . --constraints CONSTRAINTS.yaml
```

```
Правил: 3, нарушений: 1 (error: 1, warn: 0)
  [error] src/hotfix.py:2 no_pan_in_code — must_not_contain: запрещённый паттерн '\b\d{16}\b': pan = "4276550012345678"  # TODO: убрать тестовый PAN
Итог: FAIL
# exit 1
```

Находка указывает файл, строку, правило и сниппет — этого достаточно для
диагностики в пайплайне. Схема правил и остальные типы проверок —
`docs/control.md`; `--json` — машиночитаемый отчёт `FitnessReport`
(SDK-контракт v1).

## 8. Первая диаграмма

Archify — контур «архитектура как код»: JSON IR → валидация (9 artifact
checks + composition-профиль) → атомарная доставка HTML с SHA-256
receipt. Фикстуры контура СБП —
`banking/demos/cli-from-claude-code/scenario2-archify-cli`.

```
$ arch-ml archify validate architecture banking/demos/cli-from-claude-code/scenario2-archify-cli/sbp-v1.architecture.json
archify validate: ok
checks: 9/9
composition: pass (errors 0, warnings 0)
# exit 0

$ arch-ml archify deliver architecture banking/demos/cli-from-claude-code/scenario2-archify-cli/sbp-v1.architecture.json /tmp/sbp-v1.html
archify deliver: ok
validation: 9/9 checks, errors 0, warnings 0
spec: sha256 cb492b86486d8f9c2f3db3e5003ce0e09d4f557da97560a26868e3622d26a20b (7456 байт)
artifact: sha256 4ac963b6b923e88c68a5d0c78db389800a0a345706e20148943e860cd68779e1 (725537 байт)
# exit 0
```

`deliver` атомарен: HTML либо доставлен целиком с receipt, либо не
появился вовсе. Дельта двух версий спецификации — `archify compare`
(машинный diff added/removed/changed/rerouted + HTML Before/Delta/After);
методика авторинга IR — плагин ru-archify (скилл archify-diagrams).

## 9. Первый вызов из SDK

SDK — тонкие клиенты поверх headless CLI (без shell, без сети; контракт
v1 — `sdk/CONTRACT.md`). Бинарь разрешается так: параметр `binary` →
`SPINE_BE_BIN` → `arch-ml` из `PATH` (шаг 2 уже позаботился). Готовый
пример — CI-гейт на Python SDK:

```bash
python3 sdk/python/examples/ci_gate.py /tmp/arch-gate \
  --constraints /tmp/arch-gate/CONSTRAINTS.yaml
```

```
Репозиторий: /tmp/arch-gate
Сводка: Правил: 3, нарушений: 0 (error: 0, warn: 0)
ГЕЙТ: PASS
# exit 0
```

Коды выхода примера: 0 — гейт зелёный, 1 — гейт красный (нарушения —
это данные, не ошибка), 2 — ошибка исполнения (бинарь не найден, процесс
упал). Красный отчёт приходит типизированным `FitnessReport` с находками
«файл:строка — правило — сообщение»; `passed=false` — валидные данные,
не исключение. То же API — на Rust и Java (`sdk/README.md`).

## 10. Куда дальше

| Раздел | Ссылка |
|---|---|
| Портал документации | `docs/README.md` |
| Слэш-команды TUI | `docs/slash_commands.md` |
| Архитектурный контроль: триггеры, spine, fitness, гейты | `docs/control.md` |
| Справочник инструментов агента | `docs/tools.md` |
| Устройство харнесса | `docs/architecture.md` |
| Модели и провайдеры | `docs/models.md` |
| SDK: контракт и клиенты (Python/Rust/Java) | `sdk/CONTRACT.md`, `sdk/README.md` |
| Демо-сценарии (проприетарная зона) | `banking/demos/`: `cli-from-claude-code`, `archify-adf`, `sdk-embedding`, `payments`, `pangolin-migration` |

Демо-сценарии `banking/demos/cli-from-claude-code` — те же шаги, что в
этом гайде, с зафиксированными прогонами (`test-run.md` в каждом
каталоге сценария): headless-ответ, диаграммы СБП, красный/зелёный гейт.
