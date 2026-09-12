# spine-be-sdk (Python)

Тонкий Python-клиент поверх headless CLI `arch-ml` (Spine-BE — харнесс
solution-архитектора). SDK запускает процесс `arch-ml` **без shell**
(argv-массив), читает stdout/stderr и exit code, разбирает JSON по контракту
[`../CONTRACT.md`](../CONTRACT.md) (версия 1). Сетевых вызовов в SDK нет —
только `subprocess`.

Зависимости: **только стандартная библиотека** (Python ≥ 3.10).

## Подключение

Пакет не опубликован в PyPI; подключается из монорепо:

```bash
# вариант 1: установка в окружение
pip install ./sdk/python   # из корня монорепо

# вариант 2: без установки — добавить src/ в sys.path или PYTHONPATH
export PYTHONPATH=<монорепо>/sdk/python/src
```

Бинарь `arch-ml` разрешается так (§0 контракта): параметр `binary` →
переменная окружения `SPINE_BE_BIN` → `arch-ml` из `PATH`.

```python
from spine_be_sdk import SpineBE

client = SpineBE()                      # бинарь из SPINE_BE_BIN или PATH
client = SpineBE(binary="/path/to/arch-ml", timeout=120.0)  # явно
```

## Методы

### `run(prompt, model=None, timeout=None, max_turns=None)` — headless-прогон агента (LLM)

`arch-ml run -q ...` — stdout это финальный ответ ассистента (произвольный
текст, не JSON). Требует настроенного LLM-провайдера.

```python
result = client.run("черновик ADR по saga", model="deepseek-flash", max_turns=5)
print(result.answer, result.duration_ms)
```

### `control_check(repo, constraints=None, timeout=None)` — fitness-контроль

`arch-ml control check <REPO> [--constraints PATH] --json` → `FitnessReport`.
Красный гейт (`passed == False`, exit 1) — это **данные**, а не исключение.

```python
report = client.control_check("fixtures/", constraints="fixtures/CONSTRAINTS.yaml")
if not report.passed:
    for issue in report.issues:
        print(f"{issue.file}:{issue.line} [{issue.severity}] {issue.rule}: {issue.message}")
```

### `archify_validate(type, path, timeout=None)` — валидация диаграммы

`arch-ml archify validate <TYPE> <IR_PATH> --json` → receipt целиком (dict).

```python
receipt = client.archify_validate("architecture", "sbp-v1.architecture.json")
print(receipt["ok"], len(receipt["checks"]), receipt["composition"]["status"])
```

### `archify_deliver(type, path, output, timeout=None)` — рендер HTML

```python
receipt = client.archify_deliver("architecture", "sbp-v1.architecture.json", "out/sbp-v1.html")
print(receipt["artifact"]["sha256"], receipt["artifact"]["bytes"])
```

### `archify_compare(base, head, output, timeout=None)` — дельта двух версий

```python
receipt = client.archify_compare("sbp-v1.architecture.json", "sbp-v2.architecture.json", "out/delta.html")
print(receipt["summary"]["components"]["added"], receipt["summary"]["connections"]["added"])
```

Receipt'ы archify (§3 контракта) передаются вызывающему коду **целиком** как
`dict` — типизированы только задокументированные поля, остальное доступно как
generic JSON.

## Обработка ошибок (§4 контракта)

```python
from spine_be_sdk import BinaryNotFound, Timeout, ProcessFailed, ContractViolation

try:
    report = client.control_check("repo/")
except BinaryNotFound:
    ...  # arch-ml не найден (SPINE_BE_BIN/PATH) или не исполняемый
except Timeout:
    ...  # истёк клиентский таймаут, процесс убит
except ProcessFailed as e:
    ...  # ненулевой exit без валидного JSON: e.exit_code, e.stderr
except ContractViolation as e:
    ...  # stdout не парсится как JSON там, где контракт требует JSON: e.stdout
```

`control check` с `passed=false` исключения **не вызывает** — красный гейт
передаётся как `FitnessReport(passed=False)`.

## Тесты

```bash
cd <монорепо>/sdk/python
python3 -m pytest tests/ -v
```

- `tests/test_unit.py` — построение argv и маппинг ошибок на фейк-бинаре
  (заглушка-скрипт; LLM-прогон `run()` по §5 контракта не выполняется).
- `tests/test_adversarial.py` — враждебные сценарии QA: дедлок пайпов на
  мегабайтных stderr, убийство процесса по таймауту (без зомби), не-UTF-8
  локаль, промпты с `-`, относительный `SPINE_BE_BIN` + чужой `cwd`,
  приоритет per-call `timeout` над дефолтом клиента, наследование `cwd`.
- `tests/test_integration.py` — живые прогоны против `target/release/arch-ml`
  на эталонных фикстурах §5 (`scenario3-gate/fixtures`, `sbp-v1/v2`).
  Бинарь берётся из `SPINE_BE_BIN`, иначе ищется release-сборка монорепо;
  если бинаря нет — интеграционные тесты пропускаются (skip).

## Ограничения и найденное в QA (2026-09-03)

Факты, зафиксированные adversarial-прогоном (`tests/test_adversarial.py`):

- **Декодирование всегда UTF-8.** `arch-ml` (Rust) печатает UTF-8 независимо
  от локали; SDK декодирует stdout/stderr как UTF-8 с `errors="replace"`.
  До фикса под C-локалью клиент падал с `UnicodeDecodeError` на кириллице
  из контракта.
- **Промпт с ведущим `-`.** SDK вставляет разделитель `--` перед промптом,
  начинающимся с `-` (иначе clap отклоняет его как неизвестный флаг, exit 2).
  Сентинель `-` (чтение промпта из stdin, §1) передаётся без `--` — clap
  принимает одиночный дефис как позиционное значение.
- **Относительный `SPINE_BE_BIN` / `binary`.** Путь нормализуется в абсолютный
  при разрешении: иначе exec резолвил бы его от `cwd` дочернего процесса
  (параметр `cwd` клиента), а не от каталога вызывающего, и падал бы
  с вводящим в заблуждение `BinaryNotFound`.
- **Большие потоки.** `subprocess.run(capture_output=True)` читает оба пайпа
  конкурентно (`communicate()`): мегабайты мусора в stderr при валидном JSON
  в stdout дедлока не дают (покрыто тестами до 3 МБ + дедлайном pytest-timeout).
- **Таймаут убивает процесс.** По истечении клиентского таймаута
  `subprocess` делает kill+wait — зомби/сирот не остаётся (проверено
  по pid живого процесса). Per-call `timeout=` перекрывает дефолт клиента
  в обе стороны (и сужает, и расширяет).
- **`cwd` клиента** наследуется дочерним процессом; команды с относительными
  путями (`control_check(".", constraints="CONSTRAINTS.yaml")`) резолвятся
  от него — это задокументированное поведение §0, а не дефект.
- **`run()` с промптом `-`** передаёт `-` в CLI (чтение промпта из stdin
  самим `arch-ml`); SDK при этом не пишет в stdin — подавать данные на stdin
  через SDK v1 нельзя (ограничение публичного API, для длинных промптов
  передавайте строку аргументом).
