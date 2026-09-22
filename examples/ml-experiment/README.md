# examples/ml-experiment — офлайн-демо fitness-гейта за 1 минуту

Минимальный «ML-проект» (один учебный скрипт обучения) и набор правил из
пресета AI/ML-исследователя (`aiml/presets/ml-researcher/SPINE-ML.md`):

- **ML-06 — воспроизводимость прогона**: у прогона зафиксированы конфиг,
  версии стека и seed (манифест прогона);
- **ML-09 — бюджет прогона**: потолок стоимости объявлен до запуска.

Демо не требует ни API-ключей, ни GPU, ни сети — `control check` это
детерминированный слой харнесса (AD-2): проверки выполняет код, не модель.

## Прогон

Из каталога этого примера (бинарь `arch-ml` собран и в PATH, либо
подставьте путь `<репо>/target/release/arch-ml`):

```bash
arch-ml control check . --constraints CONSTRAINTS.yaml
```

Текущее состояние примера — красное, это задумано:

```
Правил: 3, нарушений: 3 (error: 3, warn: 0)
  [error] run-manifest.yaml:0 budget_declared — must_contain: паттерн 'budget' не найден ...
  [error] run-manifest.yaml:0 run_manifest_present — file_exists: файл не найден: run-manifest.yaml
  [error] train*.py:0 seed_declared — must_contain: паттерн '(?i)seed\s*=' не найден ...
Итог: FAIL     # exit 1
```

Гейт указывает файл, правило и чего не хватает — этого достаточно для
диагностики в CI.

## Исправление

Каталог `fix/` содержит исправленные версии: seed в скрипте и манифест
прогона с объявленным бюджетом:

```bash
cp fix/train.py train.py
cp fix/run-manifest.yaml run-manifest.yaml
arch-ml control check . --constraints CONSTRAINTS.yaml
```

```
Правил: 3, нарушений: 0 (error: 0, warn: 0)
Итог: PASS     # exit 0
```

## Что здесь показано

Одно и то же свойство («эксперимент воспроизводим», «бюджет под
контролем») записано один раз — и машинно исполняется на каждом коммите,
а не живёт прозой в вики. Так же устроены правила боя: корневой
`CONSTRAINTS.yaml` этого репозитория и карточки
`aiml/library/fitness/wave1-ml-experiment.yaml` (seed, пины veRL/vLLM,
`gpu_memory_utilization`, лимиты бюджета). Схема правил и все типы
проверок — `docs/control.md`.
