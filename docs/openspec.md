# Адаптер OpenSpec (`arch-ml openspec`)

Spine читает репозиторий с разметкой [OpenSpec](https://openspec.ai) как
**источник** требований и архитектурных решений, а вердикт выносит своим
детерминированным ядром (`src/openspec.rs`). Это адаптер, а не форк и не
зависимость от OpenSpec CLI: установленный OpenSpec не нужен, файлы читаются
при каждом прогоне (`--watch` не требуется).

## Что адаптер читает

| Источник | Что извлекается |
|---|---|
| `openspec/specs/<capability>/spec.md` | Требования: блоки `### Requirement:` со строками SHALL/MUST (живая истина) |
| `openspec/changes/<id>/specs/**/spec.md` | Требования дельт активных changes (контекст гейта архивации) |
| `openspec/changes/<id>/design.md` | Кандидаты в спайн: секции Decisions/Constraints — в очередь на подтверждение, **не автоматом** |
| `openspec/changes/archive/<id>/design.md` | История решений (откуда правило/решение — источник для expiry) |
| `CONSTRAINTS.yaml` (свой) | Поле `covers:` правил — связь «правило ← требование» |

Блок требования начинается заголовком `### Requirement: <title>` и
заканчивается любым заголовком уровня 1–4 (сценарии `#### Scenario:` в тело
не входят). Требования без строк SHALL/MUST отбрасываются.

## Стабильный идентификатор требования

`openspec:<capability>#<hash8>` — младшие 32 бита FNV-1a от capability и
нормализованного текста строк SHALL/MUST. Заголовок `### Requirement:` в хэш
**не входит** (заголовки OpenSpec нестабильны): переименование требования не
ломает связь, а редактирование текста требования — осознанно ломает (правило
перестаёт его покрывать, что видно в отчёте).

## Связь требование ↔ правило: `covers:`

Markdown OpenSpec адаптер **не трогает** (иначе сломается их
`openspec validate --strict`). Связь живёт на своей стороне — в поле `covers:`
правила `CONSTRAINTS.yaml`:

```yaml
constraints:
  - id: C-001
    name: no_f64_money
    type: must_not_contain
    glob: "src/**/*.rs"
    pattern: "\\bf64\\b"
    severity: critical
    covers: ["openspec:payment-authorization#1a2b3c4d"]
```

Поле опционально и обратно совместимо (`serde default`, схема —
`FitnessRule` в `src/control.rs`); движок находок его не использует — только
отчёт покрытия и гейты адаптера.

## Команды

### `arch-ml openspec scan <ROOT> [--json]`

Список требований: стабильный id, capability, заголовок, строки SHALL/MUST,
источник (файл:строка, spec или change). Markdown по умолчанию, `--json` —
`ScanReport`.

### `arch-ml openspec coverage <ROOT> [--constraints <PATH>] [--json] [--strict]`

Отчёт покрытия:

- **SHALL всего** — уникальных требований (specs + дельты активных changes,
  дубли по id слиты);
- **покрыто детектором** — есть правило с `covers:` без признака
  `unverifiable`;
- **unverifiable с owner** — только заглушки `unverifiable: true` с
  назначенным owner (осознанный долг ручного контроля);
- **без решения** — ни детектора, ни unverifiable с owner; список поимённо.

Заглушка `unverifiable` с **пустым** owner — это ещё не решение (свежий
скелет `openspec init`), требование остаётся «без решения».

Файл ограничений: `--constraints`, иначе авто-детект
`<ROOT>/.arch-handoff/CONSTRAINTS.yaml` → `<ROOT>/CONSTRAINTS.yaml`; если
файла нет — все требования «без решения» (честное состояние, не ошибка).

Exit code: 0 всегда, кроме `--strict` — тогда 1 при наличии требований
«без решения» (гейт CI).

### `arch-ml openspec init <ROOT> [--out <DIR>] [--force]`

Генерирует (= `init --from-openspec` из рекомендаций):

- `CONSTRAINTS.from-openspec.yaml` — все найденные SHALL как правила-заглушки
  `unverifiable: true` с пустым owner и проставленным `covers:`. Это черновик
  для вливания в основной `CONSTRAINTS.yaml`: заглушки исполнять нечего —
  `control check` их пропускает как записи ручного контроля
  (`docs/corp-spine.md`), но покрытие в отчёте `openspec coverage` появится
  только после заполнения детектора (type/glob/pattern) или owner;
- `SPINE.draft.md` — кандидаты из секций Decisions/Constraints design.md
  активных changes (очередь на подтверждение архитектором) и история решений
  из `changes/archive/`;
- печатает отчёт покрытия по сгенерированному скелету.

Существующие файлы не затираются без `--force`; регенерация с `--force`
детерминирована (байт-в-байт то же содержимое).

### `arch-ml openspec gate --archive <ROOT> <change-id> [--constraints <PATH>]`

Гейт архивации change (точка CI перед `openspec archive`): **exit 1**, если
хотя бы одно требование дельты `changes/<change-id>/specs/` — «без решения»,
либо падает `control check` по файлу ограничений. Иначе PASS, exit 0.

## Чего адаптер НЕ делает

- Не парсит скиллы и slash-команды OpenSpec, не исполняет их CLI.
- Не переписывает файлы OpenSpec — единственные записываемые артефакты:
  свои `CONSTRAINTS.from-openspec.yaml` и `SPINE.draft.md`.
- Не синкает спайн из design.md автоматически — кандидаты только в черновик.
- Не требует OpenSpec: адаптер живёт за подкомандой `openspec`, ядро
  (`control`, `trace`, `model`) о нём не знает и без разметки OpenSpec
  работает как раньше.

## Roadmap (за пределами MVP)

- **sync**: спайн поверх design.md — подтверждённые кандидаты из
  `SPINE.draft.md` в `ARCHITECTURE-SPINE.md` с обратной ссылкой на change.
- **gate --change <id>**: гейт активного change (proposal/tasks как контекст,
  покрытие дельты до archive).
- **gate --expiry**: правила, порождённые из archived changes, получают
  expiry/owner из истории archive; просроченные — в отчёт.
- **config.yaml rules → черновики правил**: маппинг per-artifact rules
  OpenSpec в `must_contain` по артефактам.
