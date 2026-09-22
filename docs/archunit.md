# ArchUnit-мост: JVM-гейты из единого источника (ADR-039)

Архитектор описывает структурные правила один раз — в `CONSTRAINTS.yaml`
(и/или типизированной модели `model/`), — а arch-ml исполняет их на
JVM-проектах **настоящим ArchUnit**: либо бесшовно (standalone-гейт, без
единой правки Java-репо), либо через сгенерированный JUnit-тест,
встраиваемый в репо команды. Результат — в едином отчёте гейта
(`arch-ml control check`), с id правила CONSTRAINTS в каждой находке.

Нативные проверки `dependency_direction`/`context_boundary` (ADR-029/030)
остаются и работают на всех стеках; ArchUnit-мост — JVM-нативное
исполнение **того же источника** с более сильной доказательностью
(анализ байткода: зависимости через сигнатуры полей/методов, вызовы,
аннотации — не только текстовые импорты).

## Быстрый старт

```bash
arch-ml archunit fetch                       # один раз: jar'ы в кэш (пины + SHA-256)
javac -d classes $(find src -name '*.java')  # или mvn compile / gradle classes
arch-ml archunit check . --constraints CONSTRAINTS.yaml
```

Учебный прогон end-to-end (FAIL → фикс → PASS): `кейсы/jvm-archunit-gate/`.

## Два пути исполнения

### 1. Standalone-гейт: `arch-ml archunit check <repo>` (бесшовно для репо)

Ничего не встраивается в Java-проект. Гейт сам:

1. строит спек из java-правил CONSTRAINTS.yaml (см. маппинг ниже);
2. находит скомпилированные классы: `--classes <dir>` или авто-детект
   `target/classes` (Maven), `build/classes/java/main` (Gradle),
   `out/production` (IntelliJ), `classes` (голый javac);
3. находит jar'ы ArchUnit: `--jar-dir`, затем env `ARCHUNIT_HOME` (каталог
   или его `lib/`), затем кэш `~/.arch-ml/archunit/lib`;
4. генерирует `ArchGateRunner.java` (main без JUnit; правила захардкожены
   из спека — JSON на стороне Java не парсится) и компилирует его javac'ом
   в кэш `~/.arch-ml/archunit/runner` — пересборка только при смене
   исходника (ключ кэша — SHA-256 исходника);
5. запускает `java -cp <classes>:<jars>:<runner> ArchGateRunner` с
   таймаутом (дефолт 300 с, `--timeout-secs`);
6. парсит вывод (`VIOLATION|<id>|<detail>`) в отчёт; exit 1 при
   error-нарушениях, 0 при чистом прогоне. `--json` — машиночитаемый отчёт.

### 2. Встраиваемый JUnit-тест: `arch-ml archunit gen <repo>`

```bash
arch-ml archunit gen . --constraints CONSTRAINTS.yaml --out-dir architecture-fitness
```

Артефакты:

- `ArchFitnessTest.java` — идиоматичный JUnit 5 + ArchUnit тест
  (`@AnalyzeClasses(packages = …, importOptions = DoNotIncludeTests.class)`,
  `@ArchTest`-поля; имя поля и текст `.as(…)` содержат id правила). Для
  запуска в репо команды нужна зависимость
  `com.tngtech.archunit:archunit-junit5` той же версии (1.5.0);
- `archunit-rules.json` — сериализованный спек (версия схемы 1) для
  инспекции и внешних потребителей.

Базовый пакет `@AnalyzeClasses` выводится из якорных пакетов спека
(общий префикс; единственный якорь укорачивается до родителя — иначе
классы других слоёв не попадут в анализ). Переопределение:
`--base-package com.acme`. Если якорных пакетов нет — сканируется всё
(`..`), что на больших репо медленнее: задайте `--base-package`.

## Маппинг YAML → ArchUnit

Берутся правила `dependency_direction` и `context_boundary`, чей glob
целится в `**/*.java`. Остальные типы правил ArchUnit-мосту не относятся и
пропускаются молча. Правила, которые не смаплись (не-java glob,
невыводимый пакет, сломанный forbid/allow), попадают в секцию
`unsupported` спека с причиной и печатаются предупреждением — генерацию и
гейт они не роняют.

**File-glob → пакетный паттерн** (эвристика): отбрасывается файловый
сегмент (`*.java`) и хвостовые wildcard'ы; отрезаются известные исходные
префиксы (`src/main/java/`, `src/main/kotlin/`, `app/src/main/java/`,
`src/`); wildcard-сегменты `**`/`*` удаляются. Если до конкретных
сегментов стоял `**` — паттерн свободный, иначе якорный:

| Glob | Паттерн |
|------|---------|
| `src/**/domain/**/*.java` | `..domain..` |
| `src/main/java/com/acme/domain/**/*.java` | `com.acme.domain..` |
| `**/*.java` | `..` (любой пакет) |

**Запись forbid/allow → пакетный паттерн**: слеши и точки
нормализуются; последний сегмент с заглавной буквы считается именем
класса и отбрасывается (`com/acme/infra/OrderRepository` →
`com.acme.infra..`); многосегментная запись → якорный паттерн
(`com/acme/infrastructure` → `com.acme.infrastructure..`), одиночный
сегмент → свободный (`infrastructure` → `..infrastructure..`).

**Типы правил**:

- `dependency_direction` + `forbid` →
  `noClasses().that().resideInAPackage(from).should().dependOnClassesThat().resideInAnyPackage(to…)`
  — одно правило на весь forbid-список;
- `dependency_direction` + `allow` →
  `classes().that().resideInAPackage(from).should().onlyDependOnClassesThat().resideInAnyPackage(allow… + from + JDK)` —
  allow в CONSTRAINTS описывает **внутренние** модули, поэтому
  собственный пакет и JDK-белый список (`java..`, `javax..`, `jakarta..`,
  `jdk..`, `sun..`, `com.sun..`, `org.w3c..`, `org.xml..`, `kotlin..`,
  `scala..`, `groovy..`, `org.jetbrains..`) добавляются автоматически.
  Учтите: байткод-анализ строже текстового — видны все зависимости,
  включая не требующие import;
- `context_boundary` (ADR-030) → для каждой упорядоченной пары CMP без
  `depends_on` — `ForbiddenDeps` из пакета источника в пакет цели; пакет
  CMP выводится из `code_roots` тем же отрезанием исходных префиксов.
  Модель ищется в `--model-dir` (дефолт `model`); недоступная модель —
  unsupported с причиной.

Проверка циклов (`slices()…beFreeOfCycles()`) и `layeredArchitecture()`
осознанно НЕ генерируются: из типов правил CONSTRAINTS они не выводятся
(см. ADR-039, «отклонённые дополнения»).

## Тип правила `archunit` в CONSTRAINTS.yaml

```yaml
- id: C-03
  name: jvm_archunit_gate
  type: archunit
  classes_dir: target/classes   # опционально; без него — авто-детект
  jar_dir: vendor/archunit      # опционально; без него — ARCHUNIT_HOME / кэш
  timeout_secs: 300             # дефолт 300
  severity: critical
```

`arch-ml control check` исполняет его тем же кодом, что `archunit check`:
спек строится из java-правил **того же файла**, нарушения кладутся в общий
отчёт находками с id исходного правила и его severity; unsupported —
warn-находки. Инфраструктурный сбой — error-находка (fail-closed):
гейт не может «молча позеленеть».

## Fail-closed диагностика

| Ситуация | Поведение |
|----------|-----------|
| Нет `java`/`javac` в PATH | ошибка «нужна JRE/JDK (java в PATH)» |
| Нет jar'ов ArchUnit | ошибка с подсказкой `arch-ml archunit fetch` (или `--jar-dir` / `ARCHUNIT_HOME`) |
| Нет скомпилированных классов | ошибка с подсказкой `mvn compile` / `javac -d classes …` (или `--classes`) |
| Пустой спек (нет java-правил) | ошибка «спек не содержит правил» + ссылка на unsupported |
| Таймаут (дефолт 300 с) | процесс убивается, ошибка с указанием лимита и `--timeout-secs` |
| Раннер не собрался / упал | ошибка с хвостом stderr javac/java |

## Пины версий и SHA-256 (supply-chain)

`arch-ml archunit fetch` скачивает с Maven Central в
`~/.arch-ml/archunit/lib` и проверяет SHA-256 против пинов ниже
(те же значения зашиты в код, `src/archunit.rs` `PINNED_JARS`); при
расхождении файл НЕ записывается. Повторный fetch идемпотентен: файл с
совпадающим хэшем не скачивается.

| Файл | Версия | SHA-256 |
|------|--------|---------|
| `archunit-1.5.0.jar` | 1.5.0 (текущий `<release>` maven-metadata, снято 2026-09-04) | `5ab139643fa5090af181ff8dc9eab48cb38ca306d3e03e66acacc75831a08fe9` |
| `slf4j-api-2.0.19.jar` | 2.0.19 (последний стабильный 2.0.x) | `e91ff6d720609e7a194ffe758c3ed5c84e798617ae07b0a0f6a4fe229741b4bb` |
| `slf4j-nop-2.0.19.jar` | 2.0.19 | `d0226062f9b3a2793f62002f892a8e624eb07f396130fc949e01b1d5f4888cba` |

Зачем slf4j: ArchUnit core логирует через slf4j-api — без него
`ClassFileImporter` падает с `NoClassDefFoundError` (проверено живьём);
`slf4j-nop` — тихий биндинг, чтобы гейт не шумел логами.
