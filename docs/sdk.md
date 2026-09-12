# SDK — встраивание Spine-BE

Тонкие клиенты (Python, Rust, Java) поверх headless CLI `arch-ml` для
встраивания Spine-BE в продукты и процессы команд (ADR-028): CI/CD-гейты,
pre-commit, чат-боты, кодовые агенты, внутренние сервисы банка.

## Назначение и устройство

До SDK взаимодействие с харнессом было человеческим (TUI) либо хрупким
текстовым парсингом stdout. SDK даёт стабильный машиночитаемый контракт на
языке команды. Устройство минимальное:

- SDK **не содержит логики харнесса**: запускает процесс `arch-ml` без shell
  (argv-массив), читает stdout/stderr/exit code, разбирает JSON. Сети и RPC
  нет — процесс как граница (ADR-028, вариант «в»: не демон и не линковка
  с ядром).
- Бинарь разрешается из env `SPINE_BE_BIN` → иначе `arch-ml` из `PATH`.
- Клиентский таймаут убивает зависший процесс (в Java — всё дерево процессов).
- Все три SDK реализуют одну форму API и одни ошибки; поведение зафиксировано
  кросс-языковым parity-прогоном (см. ниже).

## Контракт v1

Источник истины — [`sdk/CONTRACT.md`](../sdk/CONTRACT.md) (версия 1, совпадает
со `schemaVersion` receipt'ов Archify CLI). Выжимка:

| Точка | Команда | stdout | Exit |
|---|---|---|---|
| §1 `run` | `arch-ml run -q [PROMPT\|-]` | только финальный ответ ассистента (текст, не JSON) | 0 — успех (stderr пуст); 1 — сбой, причина в stderr |
| §2 fitness | `arch-ml control check <REPO> --json` | одна строка JSON `FitnessReport` | 0 — `passed=true`; 1 — `passed=false` (JSON напечатан!) |
| §3 archify | `arch-ml archify validate\|deliver\|compare … --json` | pretty JSON-receipt, `schemaVersion: 1` | 0 — `ok=true`; ненулевой при `ok=false`/таймауте |

Совместимость: добавление новых полей в JSON — не ломающее изменение;
переименование/удаление полей и смена exit-кодов — ломающее (major). SDK
передаёт receipt вызывающему коду целиком (типизированы только поля контракта).

## Три SDK

| SDK | Каталог | Зависимости | Тесты (прогон 2026-09-03) | README |
|-----|---------|-------------|---------------------------|--------|
| Python | `sdk/python` | только stdlib (Python ≥ 3.10) | `python3 -m pytest tests/` — 46 passed | `sdk/python/README.md` |
| Rust | `sdk/rust` | `serde` + `serde_json` (MSRV 1.85) | `cargo test --offline` — 34 passed | `sdk/rust/README.md` |
| Java | `sdk/java` | только JDK 21 (ноль внешних, встроенный MiniJson) | `./build.sh && ./test.sh` — 118 passed, 0 failed | `sdk/java/README.md` |

Подключение: Python — `pip install ./sdk/python` или `PYTHONPATH=sdk/python/src`;
Rust — path-зависимость `spine-be-sdk = { path = "sdk/rust" }`; Java —
`sdk.jar` в classpath либо копия исходников `src/bank/spine/sdk/`.

## Единая форма API (5 методов)

Имена приведены в Python/Rust-нотации; в Java — camelCase (`controlCheck`,
`archifyValidate`, …), сигнатуры — в README соответствующего SDK.

| Метод | CLI за вызовом | Результат |
|---|---|---|
| `run(prompt, model?, timeout?, max_turns?)` | `arch-ml run -q` | `{ answer, model?, durationMs }` — ответ текстом |
| `control_check(repo, constraints?)` | `arch-ml control check <REPO> [--constraints PATH] --json` | типизированный `FitnessReport`: `passed`, `summary`, `issues[]` (файл/строка/правило/severity) |
| `archify_validate(type, path)` | `arch-ml archify validate <TYPE> <IR> --json` | receipt: `ok`, 9 artifact checks, composition-профиль |
| `archify_deliver(type, path, output)` | `arch-ml archify deliver <TYPE> <IR> <OUT_HTML> --json` | receipt + SHA-256 и размер спецификации и HTML-артефакта |
| `archify_compare(base, head, output)` | `arch-ml archify compare <BASE> <HEAD> <OUT_HTML> --json` | receipt + машинная дельта версий (added/removed/changed/moved/rerouted) |

`TYPE`: `architecture|workflow|sequence|dataflow|lifecycle` (compare — только
`architecture`). `run()` требует настроенного LLM-провайдера; остальные четыре
метода детерминированы и работают без сети и ключей.

## Ошибки (§4 контракта, единые для трёх языков)

| Ошибка | Условие |
|--------|---------|
| `BinaryNotFound` | `SPINE_BE_BIN`/`arch-ml` не найден или не исполняемый |
| `Timeout` | клиентский таймаут, процесс убит |
| `ProcessFailed` | ненулевой exit без валидного JSON-контракта; несёт `exitCode` и `stderr` |
| `ContractViolation` | stdout не парсится как JSON там, где контракт требует JSON |
| `CheckFailed` | **не исключение**: результат `control check` с `passed=false` передаётся как валидные данные отчёта |

В Python исключения — классы `spine_be_sdk.errors`; в Java — `SpineBeException`
с кодом; в Rust — enum `Error`. Красный гейт SDK отличает от сбоя исполнения:
первый — данные отчёта, второй — исключение.

## Примеры встраивания

| Пример | Язык | Сценарий |
|---|---|---|
| `sdk/python/examples/ci_gate.py` | Python | архитектурный гейт для CI: `control_check` + коды выхода 0/1/2 |
| `sdk/python/examples/adf_gate.py` | Python | гейт, собираемый анкетой ADF: opened blocks анкеты A1 → отбор PAY-правил пресета → `control_check` (нужен pyyaml) |
| `sdk/python/examples/parity.py` | Python | каноническая сводка четырёх вызовов одной строкой JSON |
| `sdk/java/examples/Parity.java` | Java | каноническая сводка четырёх вызовов одной строкой JSON |
| `sdk/java/examples/ReleaseRegistry.java` | Java | реестр архитектурных версий: validate → deliver (SHA-256) → compare + журнал receipt'ов JSONL |
| `sdk/rust/examples/parity.rs` | Rust | каноническая сводка (как Parity.java) |
| `sdk/rust/examples/agent_loop.rs` | Rust | кодовый агент проверяет себя сам: «сгенерировал → гейт красный → починил → гейт зелёный → диаграмма» |

Связанный демо-пакет с записанными прогонами (три сценария, по одному на
язык): [`banking/demos/sdk-embedding/`](../banking/demos/sdk-embedding/) —
все выводы ниже воспроизводятся по SCENARIO.md каждого из трёх сценариев.

### Живой прогон: CI-гейт (Python, 2026-09-03)

```bash
export SPINE_BE_BIN=<репо>/target/release/arch-ml
WORK=$(mktemp -d) && cp -r banking/demos/cli-from-claude-code/scenario3-gate/fixtures $WORK/repo

python3 sdk/python/examples/ci_gate.py $WORK/repo --constraints $WORK/repo/CONSTRAINTS.yaml
# Репозиторий: /tmp/tmp.XXXX/repo
# Сводка: Правил: 3, нарушений: 0 (error: 0, warn: 0)
# ГЕЙТ: PASS                                   → exit 0

printf 'pan = "4276550012345678"\n' > $WORK/repo/src/hotfix.py   # «срочный хотфикс» с PAN
python3 sdk/python/examples/ci_gate.py $WORK/repo --constraints $WORK/repo/CONSTRAINTS.yaml
# Сводка: Правил: 3, нарушений: 1 (error: 1, warn: 0)
# Нарушения:
#   src/hotfix.py:1 [error] no_pan_in_code — must_not_contain: запрещённый паттерн '\b\d{16}\b': pan = "4276550012345678"
# ГЕЙТ: FAIL                                   → exit 1

rm -rf $WORK
```

Коды выхода `ci_gate.py`: 0 — гейт зелёный, 1 — красный (нарушения — данные),
2 — ошибка исполнения (нет бинаря/репозитория).

### Живой прогон: parity — одна сводка на трёх языках (2026-09-03)

Примеры `parity` (Rust/Java) и эквивалентный вызов Python SDK исполняют одну
цепочку: зелёный `control_check` на фикстуре гейта → `archify_validate` →
`archify_compare` эталонных IR СБП — и печатают каноническую сводку:

```bash
# Python (из корня репо, PYTHONPATH указывает на SDK):
PYTHONPATH=sdk/python/src python3 - <<'PY'
import json, tempfile
from pathlib import Path
from spine_be_sdk import SpineBE

fixtures = Path.cwd() / "banking/demos/cli-from-claude-code"
gate = fixtures / "scenario3-gate/fixtures"
ir1 = fixtures / "scenario2-archify-cli/sbp-v1.architecture.json"
ir2 = fixtures / "scenario2-archify-cli/sbp-v2.architecture.json"

client = SpineBE()
green = client.control_check(gate, constraints=gate / "CONSTRAINTS.yaml")
validate = client.archify_validate("architecture", ir1)
compare = client.archify_compare(ir1, ir2, Path(tempfile.gettempdir()) / "sdk-parity-delta-python.html")

print(json.dumps({
    "green_passed": green.passed, "green_issues": len(green.issues),
    "validate_ok": validate["ok"], "checks": len(validate["checks"]),
    "compare_ok": compare["ok"],
    "comp_added": compare["summary"]["components"]["added"],
    "conn_added": compare["summary"]["connections"]["added"],
}, ensure_ascii=False))
PY

# Rust:
cd sdk/rust && cargo run --offline --example parity

# Java (команды из sdk/java/README.md):
cd sdk/java && ./build.sh
javac -cp out -d out-examples examples/Parity.java
java -cp out:out-examples bank.spine.sdk.Parity <репо>
```

Все три печатают **одну и ту же сводку** (exit 0; Python добавляет пробелы
после двоеточий — особенность `json.dumps`, значения идентичны):

```json
{"green_passed":true,"green_issues":0,"validate_ok":true,"checks":9,"compare_ok":true,"comp_added":1,"conn_added":2}
```

Это и есть parity-гарантия контракта: одинаковые argv, одинаковый разбор,
одинаковые значения на любом из трёх языков — команда выбирает SDK под свой
стек, не меняя семантики интеграции.

## Ограничения v1

- Нужен установленный бинарь `arch-ml` (SDK — не самодостаточная библиотека).
- `run()` не стримит; stdin-режим (`-`) со стороны SDK не задействован.
- LLM-прогоны в автотестах SDK не выполняются (стоимость/время) — покрыты
  построением argv на фейк-бинарях; живые интеграционные тесты используют
  только детерминированные команды (`control check`, `archify`).
- Java-SDK: вложенность JSON ограничена 1024 уровнями (встроенный MiniJson;
  реальные receipt'ы контракта не глубже десятка уровней) — подробности в
  `sdk/java/README.md` («Ограничения / найденное в QA»).

## См. также

- `sdk/CONTRACT.md` — источник истины машинного контракта v1.
- `docs/headless.md` — CLI-контракт, на котором стоят SDK (§7 — граница CLI/SDK).
- `docs/tui.md` — интерактивная поверхность (§8 — выбор поверхности).
- `docs/getting_started.md` — §9: первый вызов SDK пошагово.
- `docs/adr/ADR-028-sdk-v1-tonkie-klienty-python-rust-java-poverh-headless-cli-s-json-kontraktom.md` — решение по SDK.

