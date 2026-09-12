# Spine-BE SDK v1 — Java

Тонкий клиент Spine-BE (`arch-ml`, Rust) для Java 21+. SDK запускает процесс
`arch-ml` без shell (argv через `ProcessBuilder`), читает stdout/stderr/exit
code и разбирает JSON по контракту [`../CONTRACT.md`](../CONTRACT.md) (v1).
Сетевых вызовов и внешних зависимостей нет — только JDK.

## Подключение

```bash
./build.sh          # javac → out/ → sdk.jar
```

Дальше либо добавьте `sdk.jar` в classpath, либо скопируйте исходники
`src/bank/spine/sdk/` в свой проект (пакет `bank.spine.sdk`).

Бинарь `arch-ml` резолвится так: env `SPINE_BE_BIN` → иначе `arch-ml` из PATH.

## Примеры

### run — headless-прогон агента (LLM)

```java
SpineBeClient client = new SpineBeClient();           // SPINE_BE_BIN → arch-ml
RunResult r = client.run("черновик ADR по саге",      // prompt
        "deepseek-flash",                          // model (null — дефолт бинаря)
        Duration.ofSeconds(600),                      // клиентский таймаут (+ --timeout)
        10);                                          // max_turns
System.out.println(r.answer());
```

### control check — fitness-контроль

```java
ControlReport report = client.controlCheck(
        Path.of("repo"), Path.of("repo/CONSTRAINTS.yaml"), Duration.ofSeconds(60));
if (!report.passed()) {                    // passed=false — это ДАННЫЕ, не исключение
    for (Issue i : report.issues()) {
        System.out.printf("%s:%d [%s] %s: %s%n",
                i.file(), i.line(), i.severity(), i.rule(), i.message());
    }
}
```

### archify validate

```java
ArchifyReceipt v = client.archifyValidate("architecture", Path.of("sbp-v1.architecture.json"));
System.out.println(v.ok() + ", проверок: " + v.getList("checks").size());
```

### archify deliver

```java
ArchifyReceipt d = client.archifyDeliver("architecture",
        Path.of("sbp-v1.architecture.json"), Path.of("out/sbp-v1.html"));
System.out.println("артефакт: " + d.longAt("artifact", "bytes") + " байт");
```

### archify compare

```java
ArchifyReceipt c = client.archifyCompare(
        Path.of("sbp-v1.architecture.json"),
        Path.of("sbp-v2.architecture.json"),
        Path.of("out/sbp-delta.html"));
System.out.println("components.added = " + c.longAt("summary", "components", "added"));
System.out.println("connections.added = " + c.longAt("summary", "connections", "added"));
```

`ArchifyReceipt` типизирует только общие поля (`schemaVersion`, `ok`,
`command`, `type`); весь receipt доступен целиком через `raw()`, `getMap()`,
`getList()`, `longAt(path...)` — добавление новых полей бинарём SDK не ломает.

## Обработка ошибок

Все сбои — `SpineBeException` с кодом (§4 контракта):

```java
try {
    ControlReport r = client.controlCheck(Path.of("repo"));
} catch (SpineBeException e) {
    switch (e.code()) {
        case BINARY_NOT_FOUND   -> // нет arch-ml в SPINE_BE_BIN/PATH
        case TIMEOUT            -> // клиентский таймаут, процесс убит destroyForcibly
        case PROCESS_FAILED     -> // ненулевой exit без валидного JSON: e.exitCode(), e.stderr()
        case CONTRACT_VIOLATION -> // stdout не парсится как JSON по контракту
    }
}
```

Важно: `control check` с `passed=false` (exit 1 + валидный JSON) — НЕ
исключение, а валидный `ControlReport`. Исключение только если JSON не
распарсился или exit отличен от 0/1 без валидного JSON.

## Ограничения / найденное в QA (adversarial-прогон 2026-09-03)

Поведение ниже зафиксировано тестами (`AdversarialTest`) и является
частью контракта SDK:

- **Вложенность JSON ограничена 1024 уровнями** (`MiniJson.MAX_DEPTH`).
  Парсер рекурсивный; до фикса JSON глубиной 10 000 уровней ронял поток с
  `StackOverflowError`. Теперь — понятная `JsonException`. Реальные
  receipt'ы контракта не глубже десятка уровней, запас стократный.
- **Дубликаты ключей объекта**: последний wins (`LinkedHashMap.put`);
  позиция ключа в порядке итерации — от первого вхождения.
- **Числа**: целые → `Long`, дробные/экспоненциальные → `Double`.
  `1e308`/`1E-5` — корректно; `-0` → `0L` (знак теряется), `-0.0` знак
  сохраняет; `1e999` → `+Infinity`; целое вне диапазона long →
  `JsonException` (не молчаливая порча данных).
- **Битый UTF-8 в stdout/stderr бинаря** не роняет клиент: некорректные
  байты заменяются на `U+FFFD` (поведение `CharsetDecoder` по умолчанию).
- **Промпт `run()`, начинающийся с `-`**: SDK вставляет `--` перед промптом
  (clap бинаря иначе падает с «unexpected argument», exit 2). Проверено на
  живом `arch-ml`.
- **Относительный путь к бинарю** (через `SPINE_BE_BIN` или конструктор)
  нормализуется в абсолютный при создании клиента — иначе он резолвился бы
  от cwd дочернего процесса и ломался, когда cwd отличается от cwd JVM.
- **Таймаут убивает всё дерево процессов** (`ProcessHandle.descendants()` +
  `destroyForcibly()`): голый kill прямого ребёнка оставлял сиротами
  внуков (например, `sleep` внутри shell-скрипта).
- **stderr любого объёма не блокирует клиент**: stdout и stderr читаются
  параллельными потоками-насосами (проверено на 5 МБ мусора в stderr с
  дедлайном).
- Клиент потокобезопасен для параллельных вызовов (проверено 10 потоками);
  утечек файловых дескрипторов нет (`/proc/self/fd` до/после серии вызовов).

## Тесты

```bash
./test.sh    # сборка + компиляция тестов + java bank.spine.sdk.TestRunner
```

- Юнит-тесты `MiniJson` (вложенность, массивы, unicode-эскейпы, числа, ошибки).
- Юнит-тесты клиента на фейк-бинарях (построение argv для `run` — LLM-прогон
  в тестах НЕ выполняется; коды ошибок; таймаут с реальным kill).
- Adversarial-тесты (`AdversarialTest`): лимит вложенности, дубликаты ключей,
  переполнения чисел, pipe-deadlock на 5 МБ stderr, таймаут с kill дерева
  процессов, битый UTF-8, промпт с `-`, относительный путь к бинарю,
  10 параллельных потоков, fd-leak, живой clap-разбор и error-surfacing
  (без LLM-вызовов).
- Живые интеграционные тесты против `../../target/release/arch-ml` (или
  `SPINE_BE_BIN`): control check green/red, validate (9 проверок), deliver,
  compare. Бинаря нет — skip с предупреждением, это не падение.

При любом падении `TestRunner` завершается с exit code 1.

## Состав

- `SpineBeClient` — запуск процесса и методы контракта (5 шт.).
- `RunResult` — ответ `run` (answer, model, durationMs).
- `ControlReport` + `Issue` — FitnessReport.
- `ArchifyReceipt` — обёртка над receipt'ом Archify CLI (`Map<String,Object>`).
- `SpineBeException` — коды BINARY_NOT_FOUND / TIMEOUT / PROCESS_FAILED /
  CONTRACT_VIOLATION.
- `MiniJson` — встроенный JSON-парсер без зависимостей.
