# Кейс 009 — jvm-archunit-gate (ArchUnit-гейт для JVM из CONSTRAINTS.yaml)

> Учебный кейс ArchUnit-моста (ADR-039): архитектор описывает структурные
> правила один раз в `CONSTRAINTS.yaml`, а Spine-BE исполняет их на
> JVM-проекте настоящим ArchUnit — без ручной возни в Java-репо. Кейс
> механический, без LLM; воспроизводится голым бинарём `arch-be` + JDK.

Домен: синтетика (мини-приложение заказов, три пакета-слоя). Модель LLM не
использовалась. Требования: `java`/`javac` в PATH (кейс прогонялся на
OpenJDK 21), доступ в Maven Central ровно один раз (`archunit fetch`).

## Что показывает кейс

- **Единый источник правил**: `CONSTRAINTS.yaml` кейса несёт два правила
  `dependency_direction` с java-glob'ами (C-01 слои, C-02 запрет «внешнего
  FX-пакета») и правило `archunit` (C-03). Нативная текстовая проверка и
  JVM-нативный ArchUnit-гейт читают один и тот же файл.
- **Намеренное нарушение ловится дважды**: `domain` импортирует
  `infrastructure` и `com.external.fx` — нарушения видны и нативному
  правилу (по импортам), и ArchUnit'у (по байткоду), с id правила
  CONSTRAINTS в каждой находке.
- **Бесшовный standalone-путь**: `arch-be archunit check` сам находит
  скомпилированные классы, один раз компилирует раннер в кэш и запускает
  `java` — в Java-репо ничего встраивать не нужно.
- **Встраиваемый путь**: `arch-be archunit gen` генерирует идиоматичный
  `ArchFitnessTest.java` (JUnit 5 + ArchUnit) для репо команды — с
  трассировкой id правил в именах и сообщениях.
- **Fail-closed**: без скомпилированных классов / jar'ов гейт падает с
  понятной подсказкой, а не молча зеленеет.

## Анатомия

```
CONSTRAINTS.yaml        правила: dependency_direction (C-01, C-02) + archunit (C-03)
src/com/acme/domain/Order.java              НАРУШЕНИЕ: импорты infrastructure + com.external.fx
src/com/acme/application/OrderService.java  чистый слой приложения
src/com/acme/infrastructure/OrderRepository.java  инфраструктура
src/com/external/fx/FxClient.java           синтетический «внешний FX-пакет» (запрещён)
fixed/com/acme/domain/Order.java            исправленный домен (без infrastructure/fx)
expected/                 эталонные выводы живых прогонов (сняты реально)
  gen-stdout.txt          вывод `archunit gen`
  ArchFitnessTest.java    сгенерированный JUnit-тест (целиком)
  archunit-rules.json     сгенерированный спек (целиком)
  check-fail.txt          `archunit check` на нарушении → FAIL, exit 1
  check-pass.txt          `archunit check` после фикса → PASS, exit 0
  control-check-fail.txt  `control check` на нарушении → FAIL (C-01, C-02, C-03)
  control-check-pass.txt  `control check` после фикса → PASS
```

## Сценарий воспроизведения

```bash
# 0. Один раз: jar'ы ArchUnit в кэш (пины + SHA-256 — docs/archunit.md)
arch-be archunit fetch

# 1. Скомпилировать фикстуру голым javac (классы → classes/)
cd кейсы/jvm-archunit-gate
javac -d classes $(find src -name '*.java')

# 2. Генерация артефактов (JUnit-тест для встраивания + JSON-спек)
arch-be archunit gen . --constraints CONSTRAINTS.yaml --out-dir out

# 3. Standalone-гейт на нарушении → FAIL, exit 1 (ожидаемо!)
arch-be archunit check . --constraints CONSTRAINTS.yaml
echo "exit=$?"   # 1

# 4. Фикс: домен возвращается за границу (без infrastructure и внешнего fx)
cp fixed/com/acme/domain/Order.java src/com/acme/domain/Order.java
rm -rf classes && javac -d classes $(find src -name '*.java')

# 5. Гейт после фикса → PASS, exit 0
arch-be archunit check . --constraints CONSTRAINTS.yaml
echo "exit=$?"   # 0

# 6. Единый отчёт гейта: правило archunit в общем control-прогоне
arch-be control check . --constraints CONSTRAINTS.yaml

# 7. Вернуть нарушение для следующего прогона
git checkout -- src/ 2>/dev/null || git restore src/
rm -rf classes out src/com/acme/application/OrderRepositoryPort.java
```

## Ценность для архитектора

До ArchUnit-моста правила «слой A не зависит от слоя B» для JVM жили либо в
прозе вики, либо в ArchUnit-тестах, написанных вручную в каждом Java-репо,
— и расходились с CONSTRAINTS.yaml, по которому работают остальные гейты.
Мост убирает двойную запись: архитектор правит один YAML, а исполнение на
JVM делает настоящий ArchUnit (по байткоду, не по regex), результат
приезжает в единый отчёт `control check` вместе с нативными правилами.
Нарушение трассируется обратно: id правила (C-01) — в имени @ArchTest и в
каждой строке находки.
