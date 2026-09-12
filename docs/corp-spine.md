# Корпоративный спайн: наследование CONSTRAINTS (`extends`)

Проброс контекста корпоративной архитектуры в продуктовые репозитории:
наследуются не документы, а **инварианты** — слоями, с версионированием,
override только через ADR и отчётностью вверх. Реализация — `src/control.rs`
(механика `control check` / `control report`).

Карта уровней и механизмов (ДКА → доменные пресеты → продуктовый репо):
[diagrams/inheritance-layers.html](diagrams/inheritance-layers.html)
(интерактивная; IR — [diagrams/inheritance-layers.architecture.json](diagrams/inheritance-layers.architecture.json)).

## Три уровня

1. **Корпоративный** (владелец ДКА, отдельный репо, 5–10 инвариантов для
   любого репо банка): тех-радар (Hold), обязательные платформы, контуры
   ПДн/ПОД, утверждённые интеграционные паттерны, наблюдаемость.
2. **Доменный/платформенный** — наследует корпоративный, добавляет свои.
3. **Продуктовый** — нынешний `CONSTRAINTS.yaml` репозитория.

Цепочка наследования транзитивна: продуктовый файл с `extends` на доменный
получает и корпоративные правила.

## `extends` и версионирование

Продуктовый файл объявляет родителей одной строкой:

```yaml
extends: ["../corp-spine/CONSTRAINTS.corp.yaml@2026.3", "domain-payments@1.4"]
```

Формат записи — `<ref>@<пин версии>` (пин обязателен, разделитель —
последний `@`). Резолв ref (без сети):

- ref с `/`, ведущей `.` или расширением `.yaml`/`.yml` — **путь**
  относительно каталога текущего файла;
- «голое» имя — **реестр**: `<ref>.yaml`/`<ref>.yml` в каталоге из
  `ARCH_CONSTRAINTS_REGISTRY`, иначе `constraints.d/` рядом с текущим файлом.

У родительского файла — поле верхнего уровня `version: "2026.3"`. Семантика
пина — **«PR с diff», а не молчаливая поломка**:

- пин совпал с `version` родителя — правила наследуются молча;
- **расхождение** (родитель обновился до 2026.4, пин остался 2026.3) —
  error-находка `extends: родитель обновился: …@2026.3 → 2026.4 —
  перепиновать осознанно`. Гейт падает, пока владелец репо явно не
  перепинуется на новую версию (это и есть ревью диффа корп-изменений);
- у родителя нет `version` — error-находка (пин проверить нельзя);
- родитель не найден или цикл `extends` — ошибка загрузки.

Унаследованные правила получают **метку источника** (`<имя>@<версия>`),
видимую в выводе `control check` (строка «Источники правил: …») и в
`control report` (карта `inherited`). Собственное правило с тем же `name`,
что унаследованное, **замещает** его (shadowing). Файлы без `extends`
работают как раньше (обратная совместимость).

## Детектор тех-радара: `deny_dependency`

Первая волна внедрения (детерминирован, не спорен, покрывает все репо):

```yaml
- id: C-CORP-001
  name: tech_radar_hold
  type: deny_dependency
  deny: [left-pad, openssl-sys, log4j-core]
  reference: "Решение техкомитета 2026-03, протокол №12"
  severity: block
```

- `manifests` — glob'ы манифестов; пустое = auto: `**/Cargo.toml`,
  `**/pom.xml`, `**/requirements.txt`;
- разбор построчный, без новых зависимостей: `Cargo.toml` — секции
  `[dependencies]`/`[dev-dependencies]`/`[build-dependencies]` (и
  `*.dependencies`), имя до `=`; `pom.xml` — все `<artifactId>` (эвристика
  MVP: собственный artifactId проекта тоже виден — не давайте проекту имя
  deny-пакета); `requirements.txt` — имя до `==`/`>=`/…;
- совпадение — находка `deny_dependency: пакет '…' из deny-списка —
  основание: <reference>` (severity правила).

Что не механизуется — `unverifiable: true` с owner (поле `type` можно
опускать): движок такую запись не исполняет, но она видна в
`control report` (список `unverifiable_rules`) как осознанный долг ручного
контроля.

## `severity: block|warn`

`block` (дефолт, синоним текущего `error`) — находка ломает гейт (exit 1);
`warn` — только в отчёт. Первое внедрение корп-правил рекомендуется с
`warn` (иначе положит всем CI), перевод в `block` — отдельным решением.

## Override только через ADR

```yaml
overrides:
  - rule: C-CORP-001      # id или name правила (в т.ч. унаследованного)
    adr: ADR-041
    until: "2027-01"      # YYYY-MM (по месяцу включительно) или YYYY-MM-DD
```

- **все три поля обязательны**: неполный override — error-находка, правило
  НЕ отключается (некорректный `until` — тоже error);
- активный override отключает правило и виден в выводе
  (`[override:active] …`) и в отчёте — исключение датировано и обосновано;
- **истёкший** `until` — warn-находка «override истёк … — правило снова
  действует»: исключение протухает, правило применяется снова;
- override на несуществующее правило — warn-находка (игнорируется).

## Отчёт вверх: `control report`

```bash
arch-ml control report <repo> --level corp --json
arch-ml control report <repo> --level all            # markdown для чтения
```

`--level corp` — скоуп только унаследованных правил (взгляд ДКА), `all` —
все. JSON-контракт (аддитивный, SDK v1):

```json
{
  "rules_total": 5,
  "own": 0,
  "inherited": {"CONSTRAINTS.corp@2026.3": 5},
  "pass": 4, "fail": 0, "warn": 0,
  "passed": true,
  "overrides": [{"rule": "C-CORP-001", "adr": "ADR-041", "until": "2027-01",
                 "status": "active", "note": "…"}],
  "expired_rules": [],
  "unverifiable_rules": ["observability_standard"],
  "version_mismatches": []
}
```

`pass`/`fail`/`warn` — исходы правил (error-находки / только warn / чисто).
Агрегат ДКА («214 репо PASS, 31 override, 9 EXPIRED») строится внешним
сбором этих JSON по флоту репозиториев — вне MVP.

## Образец

`examples/corp-spine/`: корп-спайн `CONSTRAINTS.corp.yaml` (5 правил:
тех-радар, ПДн-маска, evidence ПДн-ревью, unverifiable наблюдаемость,
гигиена) + продуктовая фикстура `product/` с `extends`, override и
намеренным deny-hit (`left-pad`), покрытым активным override.

## Сценарии для демо (на копии примера)

```bash
cp -r examples/corp-spine /tmp/corp-demo && cd /tmp/corp-demo

# 1. Базовый PASS: наследование видно, override активен
arch-ml control check product --constraints product/CONSTRAINTS.yaml

# 2. «Родитель обновился»: поднимите version в CONSTRAINTS.corp.yaml до
#    2026.4 → error-находка, гейт FAIL до осознанной перепиновки

# 3. «Override истёк»: until: "2025-01" в product/CONSTRAINTS.yaml →
#    warn «override истёк», правило снова действует → deny-hit, FAIL
```

## Порядок внедрения (из рекомендаций)

1. **Тех-радар** `deny_dependency` — первым (детерминирован, не спорен).
2. **Ландшафт** — `dependency_direction` между системами (готовый тип
   правила, ADR-029).
3. **ПДн** — третьим: `must_not_contain` по маскам вне отмеченных модулей +
   обязательный `evidence/pdn-review.json`; стартовать с `severity: warn`.

## Roadmap (за пределами MVP)

- Агрегатор ДКА по флоту репо (сбор `report --json`, сводка PASS/override/
  EXPIRED) — внешний скрипт или отдельная команда.
- Policy-тест исходящих вызовов (хосты из реестра интеграций) — новый тип
  правила.
- deny по версиям/диапазонам (`deny: [{name, below}]`), нормализация имён
  pip (PEP 503), точный разбор pom.xml (исключить project/parent).
