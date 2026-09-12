# СПЕК: PLAYBOOK-стадия дистилляции (дистиллят → playbook → скилл)

- Статус: проект (P1 моста библиотеки Ариадна, `aiml/notes/library-bridge.md`,
  строка «Playbook-стадия — промежуточный слой»).
- Дата: 2026-09-11. Автор: волна P1 (playbook).
- Грунт:
  `library/distillate/1_методология/концепт-playbook — роль между дистиллятом и скиллом.md`,
  `library/distillate/1_методология/distillate — библиотека дистиллята и критерий md vs skill.md`,
  `library/distillate/1_методология/концепт-фронтматтер-в-markdown.md`,
  `library/distillate/1_методология/рубрики в оценке и обучении — коллекционирование…md`.
- Точки интеграции: `src/distill.rs` (маршрут + запись + счётчик + градация),
  `src/control.rs` (предикат градации + `RuleKind::PlaybookGraduation`),
  `assets/prompts/skill_distiller.md` (шаг 0), `assets/rubrics/playbook_graduation.yaml`
  (судейская рубрика), `src/agent/slash.rs` (`/playbook`), `CONSTRAINTS.yaml` (правило).
- Зависимости: **новых крейтов нет.** SHA-256 — уже в ядре
  (`crate::archunit::sha256_hex`, `archunit.rs:1205`). YAML — `serde_yaml_ng`
  (уже используется). `Regex` в `control.rs` уже импортирован.
- Смежный спек: `aiml/notes/p1-frontmatter-spec.md` (волна «frontmatter»).
  Он владеет **эмиссией паспорта** (`type/status/source/concepts`) и распознаванием
  `type: playbook` из тела `Md` с `uses:`/`skill_candidate:`. Этот спек владеет
  **стадией playbook как жизненным циклом**: отдельный маршрут, запись в
  `distillate/playbooks/`, счётчик `uses`, проверка градации, `[graduated]`.
  Реализовывать можно независимо: здесь используется только `frontmatter_field`
  (уже есть в `control.rs`) + локальный `upsert_fm_field`; когда `frontmatter.rs`
  из смежного спека появится — эти два хелпера переезжают туда.

---

## 0. Проблема (что закрываем)

Сегодня гейт «md vs skill» знает **две** рабочие ветки: `skill` и `md`
(`src/distill.rs:35-45`). Всё, что не дозрело до скилла, сваливается в один
`distillate/<slug>.md` и теряет различие между двумя принципиально разными
артефактами:

| # | Дефект | Где | Последствие |
|---|---|---|---|
| 1 | Нет ветки «процедура, ещё меняется» | `DistillRoute` (`distill.rs:35`) | Декларация («что это?») и процедура-в-вызревании («что делать?») пишутся одинаково → площадка вызревания неотличима от справки |
| 2 | Счётчика повторений нет | frontmatter md не пишется вообще | «Правило трёх повторений» (`knowledge-routing` SKILL.md) не имеет представления в данных: `uses` негде хранить и нечем инкрементировать |
| 3 | Нет проверки градации | `distill_to_skill` пишет скилл по одному мановению модели | Скилл градируется из **неустоявшейся** процедуры — «замороженная ошибка, которую агент уверенно повторит» (антипаттерн методологии) |
| 4 | Нет архива переросшего playbook | — | После градации playbook либо удаляется (теряется provenance «почему именно так»), либо остаётся висеть кандидатом в скилл навсегда |

---

## 1. Грунт: правила методологии (дословно)

- «**playbook** — площадка вызревания процедуры: ещё не скилл (меняется от
  применения к применению), уже не просто заметка (отвечает на вопрос „что мне
  делать?“)».
- «Процедура совершена 1-й раз → **playbook `[draft]`** → применения 2–3 →
  `[stable, skill_candidate: true]` → **SKILL** → playbook архивируется как
  **`[graduated]`**».
- «Помечай кандидатов заранее… ставь во frontmatter `skill_candidate: true` и
  счётчик применений (`uses`)».
- «Правило трёх повторений: скилл делают, когда одну инструкцию выдали агенту в
  **третий раз** и она **перестала меняться**. До этого — playbook».
- «Скилл, вырезанный из нестабильной процедуры, — замороженная ошибка».
- «**После градации playbook не удаляется**» — архивируется с указателем на скилл
  (сохраняет контекст, отвергнутые варианты, «почему именно так»).
- «Три повторения процедуры — скилл. **Три повторения ошибки — хук**» — критерий
  градации счётный; в хук уходит только механическая проекция.
- «Поле `type` (`concept | digest | synthesis | playbook`) определяет **папку и
  пайплайн** документа» → playbook живёт в `distillate/playbooks/`.
- «`дистиллят (знание) → playbook (процедура на человеческой скорости) → скилл
  (процедура агента) → хук (enforcement)`».

**Честная граница (из методологии и `mdvs-skill-spec.md` §4.3):** «стабильность
шагов» семантически недоказуема. Данный спек вводит **механический прокси** —
журнал `steps_sha256` (хэш раздела «Методика» на момент применения): счётчик
`uses` инкрементируется только при **неизменном** теле методики. Меняется
процедура → счётчик обнуляется (часы трёх повторений начинаются заново). Текст
находок так и формулируется: «повторений по журналу: N», а не «процедура
стабильна». Семантику держат судейская рубрика (§5.3) и промпт (§7).

---

## 2. Модель жизненного цикла

```
материал ──distill──▶  ┌─ skill   (процедура устоялась: uses≥3 и шаги не менялись)
                       ├─ md      (декларация: «что это?»)
                       ├─ playbook(процедура, ещё меняется) ──┐  ← НОВАЯ ВЕТКА
                       └─ none
                                                              │
                    playbook_use (применение) ──────────────▶  uses += 1
                                                              │
                    graduate_playbook ◀── проверка градации ──┘
                       │  uses ≥ 3 · skill_candidate · steps_sha256 не менялся
                       ├─▶ skills/<slug>/SKILL.md      (запись через контракт скилла)
                       └─▶ playbook: [graduated] + graduated_to → скилл (НЕ удаляется)
```

Состояния playbook (поле `status`):

| `status` | Кто ставит | Условие |
|---|---|---|
| `draft` | харнесс (эмиссия) | первая запись, `uses: 1` |
| `stable` | **человек** (после ревью) | `uses ≥ 2`, процедура не менялась |
| `graduated` | харнесс (градация) | записан скилл, `graduated_to` проставлен |

Асимметрия доверия как в frontmatter-спеке: `stable`/`graduated` харнесс сам не
«повышает»; `graduated` — следствие механической проверки градации, а не оценка
качества.

---

## 3. (а) Расширение маршрута до `Playbook`

### 3.1. Вариант enum (`src/distill.rs:35-45`)

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DistillRoute {
    /// Синтез Agent Skill → `<plugin>/skills/<slug>/SKILL.md`.
    Skill,
    /// Декларативная справка → `<plugin>/distillate/<slug>.md`.
    Md,
    /// Площадка вызревания процедуры → `<plugin>/distillate/playbooks/<slug>.md`.
    /// Отдельная ветка: тело несёт frontmatter `type: playbook`, `uses`,
    /// `skill_candidate` и журнал стабильности `steps_sha256` (§4).
    Playbook,
    /// Материала на артефакт нет — ничего не пишется.
    None,
}

impl DistillRoute {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Skill => "skill",
            Self::Md => "md",
            Self::Playbook => "playbook",
            Self::None => "none",
        }
    }
}
```

### 3.2. Разбор маркера (`parse_route`, `distill.rs:198-215`)

В `match value.trim().to_ascii_lowercase().as_str()` добавляется arm:

```rust
"md" => DistillRoute::Md,
"playbook" => DistillRoute::Playbook,
"none" => DistillRoute::None,
_ => DistillRoute::Skill,   // обратная совместимость: без маркера — скилл
```

Маркер: `<!-- routing: playbook -->` (первая строка ответа; `parse_route` уже
смотрит первые 5 строк и `strip_route_marker` снимает строку — менять их не надо).

### 3.3. Выбор цели и тела (`distill_to_skill`, `distill.rs:140-186`)

Новая ветка `match route`:

```rust
DistillRoute::Playbook => {
    let target = plugins_root
        .join(plugin)
        .join("distillate")
        .join("playbooks")
        .join(format!("{slug}.md"));
    // Паспорт playbook синтезируется/дополняется здесь, а не голым телом:
    // type/status/name/description/uses/skill_candidate/steps_sha256.
    let body = stamp_playbook(&payload, &slug);
    (target, body)
}
```

Ветка `Md` меняет только путь `distillate/playbooks/` → не меняет (остаётся
`distillate/<slug>.md`). `None` и `Skill` — без изменений.

### 3.4. Изоляция чужого плагина

Существующая защита (`distill.rs:174-181`, `if target.exists() && plugin !=
DISTILL_PLUGIN_DEFAULT`) распространяется на новый путь автоматически — она
проверяет `target`, а не маршрут. Ничего менять не нужно; в спеку — как инвариант,
тест §10 (`graduate_protects_foreign_plugin`).

---

## 4. (б) Запись playbook в kb: frontmatter `type: playbook` + `uses` + журнал

«Запись в kb» означает: файл ложится под каталог из `[knowledge] dirs`
(`KnowledgeConfig::dirs`, `config.rs:255`), поэтому `kb_search` его находит
(`kb.rs` обходит `dirs` через walkdir). Frontmatter-blind сам `kb_search` — но
`type/uses/skill_candidate` в паспорте читают **фильтры** из frontmatter-спека и
функции этого спека.

### 4.1. Паспорт playbook (эмиссия)

```yaml
---
type: playbook
status: draft
name: <slug>
description: <1–2 предложения: что делает процедура и когда применять>
uses: 1
skill_candidate: true
steps_sha256: <sha256 раздела «Методика» на момент применения>
created: <YYYY-MM-DD>
source: <первоисточник, если известен>
---
```

Инварианты:
1. `type: playbook` ставится **всегда** (даже если модель не вернула паспорт).
2. `status: draft` — `stable`/`graduated` из ответа модели форсится в `draft`
   (человек поднимет; градацию проставит харнесс, §6).
3. `uses` — целое ≥ 1; отсутствует/нечисло → `1`.
4. `skill_candidate: true` по умолчанию (playbook — кандидат по определению),
   кроме случая, когда модель явно вернула `skill_candidate: false`.
5. `steps_sha256` — вычисляется из тела, не берётся у модели.

### 4.2. Сигнатуры (`src/distill.rs`)

```rust
/// Синтезирует/дополняет паспорт playbook: `type/status/name/description/uses/
/// skill_candidate/steps_sha256`. Существующий валидный блок не пересобирается
/// целиком — только строковый upsert недостающих ключей (как `ensure_frontmatter`).
fn stamp_playbook(raw: &str, slug: &str) -> String;

/// Секция «Методика» (синонимы: методика/метод/процедура/порядок/шаги/how to) —
/// тело без заголовка, обрезанное до следующего заголовка `^#{1,6} `.
/// `None`, если раздела нет (тогда `steps_sha256` не пишется).
fn methodology_body(text: &str) -> Option<String>;

/// Хэш устойчивости шагов: `sha256_hex(methodology_body(text))`.
/// `None`, если методики в тексте нет.
fn steps_hash(text: &str) -> Option<String>;

/// Строковый upsert скалярного поля frontmatter (заменяет строку `key:` или
/// вставляет перед закрывающим `---`). Единый с `ensure_frontmatter`.
fn upsert_fm_field(text: &str, key: &str, value: &str) -> String;

/// «Процедура применена ещё раз»: инкрементирует `uses` при неизменной
/// методике, иначе обнуляет счётчик и перезакрепляет `steps_sha256`
/// (правило трёх повторений: изменённая процедура считается с нуля).
/// Возвращает новое значение `uses`.
pub fn bump_playbook_uses(path: &Path) -> Result<u32>;
```

`bump_playbook_uses` — meханика:

```
cur = steps_hash(text)?              // None → HarnessError::Tool (не playbook)
rec = frontmatter_field(fm, "steps_sha256")
uses = if rec == cur { old_uses + 1 } else { 1 }
text = upsert_fm_field(text, "uses", &uses.to_string())
text = upsert_fm_field(text, "steps_sha256", &cur)
write_atomic(path, text)
```

Чтение frontmatter — через `crate::control::frontmatter_field` (сделать
`pub(crate)`; уже реализован, `control.rs:2709`). `upsert_fm_field` — рядом с
`ensure_frontmatter`. Запись — tmp+rename (`p1-managed-spec` §: «atomic write =
tmp+rename»), без частичных файлов.

### 4.3. Счётчик против «удобства»

`uses` инкрементируется **только** явным актом применения (`playbook_use` / слэш,
§8), а не каждым повторным `/distill`. Повторная дистилляция того же материала с
тем же телом методики — тоже применение (одна и та же процедура, ещё одно
свидетельство); смена шагов — обнуление. Это и есть «правило трёх повторений в
данных».

---

## 5. (в) Рубрика/проверка градации ПЕРЕД записью в `skills/`

Две половины: **механический предикат** (жёсткий гейт в коде) и **судейская
рубрика** (семантическая, для человека/eval-судьи). Обе исполняются ДО записи в
`skills/`; предикат — обязателен, рубрика — рекомендательна.

### 5.1. Механический предикат (`src/control.rs`)

```rust
/// Нарушения допуска playbook к градации в скилл. Пустой список — playbook
/// готов по механическим признакам (семантику держат рубрика §5.3 и человек).
///
/// Проверки (структурные прокси «правила трёх повторений»):
/// 1. frontmatter есть, `type: playbook`;
/// 2. `uses` есть, целое, ≥ 3 (порог `PLAYBOOK_USES_MIN`);
/// 3. `skill_candidate` не выставлен в `false`;
/// 4. `steps_sha256` присутствует (журнал стабильности велся) — иначе
///    стабильность механически недоказуема, а не «процедура нестабильна»;
/// 5. раздел «Методика» есть и содержит ≥ 3 шага (тот же признак, что в
///    `skill_contract_violations`, control.rs:2568);
/// 6. `graduated_to` ещё не проставлен (повторная градация — не молчаливая).
pub(crate) fn playbook_graduation_violations(text: &str) -> Vec<String>;

/// Порог правила трёх повторений (в данных — счётчик `uses`).
pub(crate) const PLAYBOOK_USES_MIN: u32 = 3;
```

Проверка 4 честно отделяет «журнала нет» от «процедура менялась»: обнуление
счётчика (§4.2) само по себе гарантирует, что `uses ≥ 3` достигнуто на одном
`steps_sha256`. Хэш-равенство при градации проверяет **вызывающий** код (§6), т.к.
ему нужен доступ к телу, а не только к паспорту.

### 5.2. Fitness-правило `RuleKind::PlaybookGraduation`

**Тип:** новый `RuleKind::PlaybookGraduation` → YAML `type: playbook_graduation`.
Семантика (структурные инварианты репозитория, суждений нет):

| # | Проверка | Находка |
|---|---|---|
| 1 | playbook с `graduated_to: X` → файл `X` существует в репо | «graduated playbook указывает на несуществующий скилл» |
| 2 | playbook с `graduated_to: X` имеет `uses ≥ 3` | «архивирован без трёх повторений» |
| 3 | playbook с `skill_candidate: true` **не** обязан быть graduated (это не ошибка) | — |

Точки правки (`src/control.rs`, по образцу `SkillContract`):
- `enum RuleKind` (стр. 892) — вариант `PlaybookGraduation`;
- `impl RuleKind::as_str` (стр. 940-942) — `Self::PlaybookGraduation => "playbook_graduation",`;
- исполнитель (match по `rule.kind`, рядом с `SkillContract` на стр. 2513) —
  `collect_files` по glob (дефолт `**/distillate/playbooks/*.md`), по каждому —
  `playbook_graduation_violations` + проверка ссылки `graduated_to`;
- пустой набор файлов по glob — находка (та же конвенция, что у
  `EachFileMustContain`/`SkillContract`).

YAML-текст в `CONSTRAINTS.yaml` (рядом с `ML-SKILL-1`, стр. 527):

```yaml
- id: ML-PLAYBOOK-1
  name: playbook_graduates_into_existing_skill
  type: playbook_graduation
  glob:
    - "aiml/plugins/*/distillate/playbooks/*.md"
    - "assets/plugins/*/distillate/playbooks/*.md"
  severity: medium
  owner: "ml-architecture"
  trigger: "появление/правка playbook в plugins/*/distillate/playbooks/"
  rationale: >-
    Playbook — площадка вызревания процедуры. Скилл собирают из процедуры,
    повторённой не менее трёх раз и переставшей меняться (правило трёх
    повторений). Градация ниже порога — замороженная ошибка; graduated-указатель
    в никуда ломает provenance архива.
  evidence: "playbook с frontmatter type/uses/skill_candidate/steps_sha256 и [graduated]"
  reversibility: "обратимо"
  effort_hours: 0.5
```

### 5.3. Судейская рубрика `assets/rubrics/playbook_graduation.yaml`

Формат — как у `handoff_quality.yaml` (name/description/scale_max/origin/criteria
с anchors 1/3/5). Оценивает семантику (её не выразить regex'ом), вызывается
человеком/eval-судьёй **перед** подтверждением градации:

```yaml
name: playbook_graduation
description: >-
  Готовность playbook стать Agent Skill: процедура повторялась, перестала
  меняться и обобщается за пределы одного случая.
scale_max: 5
origin: "library/distillate/1_методология/концепт-playbook — роль между дистиллятом и скиллом.md"
criteria:
  - id: repeatability
    name: Повторяемость триггера
    weight: 2
    description: "Задача-триггер конкретна и встречается регулярно, а не единожды."
    anchors: {1: "разовый случай", 3: "повторится ещё раз", 5: "регулярная задача класса"}
  - id: stability
    name: Устойчивость шагов
    weight: 2
    description: "Шаги не менялись между применениями (сверять с журналом steps_sha256)."
    anchors: {1: "шаги переписаны", 3: "мелкие правки", 5: "шаги идентичны"}
  - id: generality
    name: Обобщаемость
    weight: 1
    description: "Процедура переносима на другие случаи, а не сшита под один прогон."
    anchors: {1: "один случай", 3: "узкое семейство", 5: "широкий класс задач"}
  - id: procedure_shape
    name: Форма процедуры
    weight: 1
    description: "Есть нумерованная методика из ≥3 шагов, а не пересказ теории."
    anchors: {1: "нарратив", 3: "частичные шаги", 5: "чёткая пошаговая методика"}
thresholds:
  min_total: 4.0
  unstable_stdev: 1.0
```

Правило методологии: «критерий, требующий суждения, в хук не уходит никогда» —
поэтому рубрика живёт как `assets/rubrics/*.yaml` (данные карточки), а не как
код; механический предикат §5.1 остаётся узким и честным.

---

## 6. (г) Градация и `[graduated]`-архивация

### 6.1. Сигнатура (`src/distill.rs`)

```rust
/// Градация playbook в Agent Skill: проверка §5.1 → запись `skills/<slug>/SKILL.md`
/// через контракт скилла → пометка `[graduated]` на playbook (архив, НЕ удаление).
/// Порядок обязателен: скилл записывается ПЕРВЫМ; архив — только после успеха.
pub async fn graduate_playbook(
    playbook_path: &Path,
    plugin: &str,
    plugins_root: &Path,
) -> Result<DistillOutcome>;
```

Тело (порядок шагов — инвариант):

1. Прочитать playbook; `methodology_body`/`steps_hash` — если методики нет →
   `HarnessError::Control("не процедура: нет раздела «Методика»")`.
2. `playbook_graduation_violations(&text)` → непусто →
   `HarnessError::Control(format!("playbook не готов к градации: {}", …))`.
3. **Хэш-равенство:** `frontmatter_field(fm, "steps_sha256") == steps_hash(text)`.
   Не равно → `Control("шаги менялись — повторений по журналу меньше трёх")`.
4. Собрать текст скилла: тело playbook уже имеет «Когда применять»/«Методика» —
   пропустить через `ensure_frontmatter(&payload, &slug)` + переопределить
   `type: skill`, `status: draft` (+ перенести `description`); прогнать
   `crate::control::skill_contract_violations` — непусто → `Control` (тот же
   гейт, что в `distill_to_skill`).
5. Записать `skills/<slug>/SKILL.md` (tmp+rename).
6. **Архивация:** `upsert_fm_field(status, "graduated")`, `upsert_fm_field(
   "graduated_to", "<plugin>/skills/<slug>/SKILL.md")`, `upsert_fm_field(
   "graduated_at", <ISO-дата>)`; первой строкой тела (после frontmatter) —
   баннер `> [graduated] → skills/<slug>/SKILL.md`. Файл **остаётся на месте** в
   `distillate/playbooks/` (grep/kb видят provenance).
7. Возврат `DistillOutcome { path: <skill path>, routed: DistillRoute::Playbook, … }`.

Идемпотентность: повторный вызов на уже `graduated` playbook → предикат §5.1
п.6 отклоняет (не молчаливая перезапись). Удаление playbook — **никогда** (только
`_archive/` по явной ручной команде, вне этой волны).

### 6.2. Тело скилла и архив

Playbook уже несёт готовые разделы («Когда применять», «Методика», «Чек-лист»,
«Антипаттерны») — скилл собирается из них без новой LLM-дистилляции
(а `skill_contract_violations` подтверждает структурную состоятельность). Это
дешевле и сохраняет «почему именно так» (отвергнутые варианты остаются в playbook).

---

## 7. Промпт `assets/prompts/skill_distiller.md` (шаг 0)

Шаг 0 сейчас знает `skill|md|none`; добавляется четвёртая ветка и различие
`md` ↔ `playbook`:

```markdown
<!-- routing: playbook -->  — процедура, которую ты применяешь/применял, но
которая ещё МЕНЯЕТСЯ между применениями (шаги каждый раз чуть другие), либо
применена впервые. Это площадка вызревания: `distillate/playbooks/`.
<!-- routing: md -->  — декларативное знание («что это?»), справка.
```

Формат результата для `playbook` (добавить в блок «Формат результата»):

```markdown
---
type: playbook
status: draft
name: <имя из задания, kebab-case, без изменений>
description: <1–2 предложения: что делает процедура и когда применять>
uses: 1
skill_candidate: true
source: <первоисточник, если известен>
---
```

И правила (дополнить абзац «Правила»):
- «`playbook` — только для **процедуры**; декларация — `md`; скилл — только когда
  процедура повторена ≥3 раз и шаги перестали меняться (`routing: skill`).»
- «`uses` ставь `1` — счётчик применений ведёт харнесс, не повышай его сам.»
- «`status` — `draft`; `stable`/`graduated` ставит человек/харнесс.»
- «`steps_sha256` не пиши — его вычисляет харнесс из раздела «Методика».»

Тест `assets.rs:1074-1090` («все SKILL.md начинаются с frontmatter») остаётся
зелёным — правится только промпт, шаблоны `assets/` не трогаются.

---

## 8. Инструменты и слэш-команды

### 8.1. `skill_distill` (`distill.rs:285-352`)

- В описании spec (стр. 291) добавить «…или playbook вызревания процедуры в
  `distillate/playbooks/`».
- Глагол вывода (стр. 339-342): добавить arm
  `DistillRoute::Playbook => "сохранено как playbook (площадка вызревания)"`.
- Возвращаемый текст для playbook дополнить подсказкой: «применений: N; градация
  в скилл — /playbook graduate <slug> (нужно ≥3)».

### 8.2. Новый инструмент `playbook_graduate`

```rust
struct PlaybookGraduateTool { plugins_root: Option<PathBuf> }

// spec(): name = "playbook_graduate"
//   args: { name: string (slug), plugin?: string }
//   description: "Проверить готовность playbook (uses ≥ 3, шаги не менялись) и
//     градировать его в Agent Skill. Playbook архивируется как [graduated]."
```

Регистрация: `distill::tools(cfg)` возвращает
`vec![SkillDistillTool, PlaybookGraduateTool]` (стр. 273-277).

### 8.3. Слэш-команда `/playbook` (`src/agent/slash.rs`)

`/playbook list | use <slug> | graduate <slug> [plugin]`:
- `list` — playbook-кандидаты под `plugins/*/distillate/playbooks/*.md` с
  колонками `uses / status / candidate` (паспорт без чтения тела);
- `use <slug>` — `bump_playbook_uses` (§4.2), вывод «применений: N/3»;
- `graduate <slug>` — `graduate_playbook` (§6), вывод пути скилла и пометки архива.

`/distill` (стр. 1005-1075) — без структурных изменений: новый маршрут приходит
из `distill_to_skill`, ветка вывода `Ok(outcome)` использует
`outcome.routed.as_str()` (дополнить сообщением для playbook).

---

## 9. Какие файлы править

| Файл | Правка | Обязательность |
|---|---|---|
| `src/distill.rs` | `DistillRoute::Playbook` + `as_str`; arm в `parse_route`; ветка в `distill_to_skill`; `stamp_playbook`/`methodology_body`/`steps_hash`/`upsert_fm_field`; `bump_playbook_uses`; `graduate_playbook`; `PlaybookGraduateTool`; глагол вывода `skill_distill`; тесты (§10) | **да** |
| `src/control.rs` | `playbook_graduation_violations`, `PLAYBOOK_USES_MIN`; `RuleKind::PlaybookGraduation` + `as_str` + исполнитель; `frontmatter_field` → `pub(crate)`; тесты | **да** |
| `assets/prompts/skill_distiller.md` | Шаг 0: ветка `playbook`; формат-паспорт; правила | **да** |
| `assets/rubrics/playbook_graduation.yaml` | Новая судейская рубрика (§5.3) | **да** |
| `src/agent/slash.rs` | `/playbook list\|use\|graduate`; сообщение в `cmd_distill` | да (для цикла применения) |
| `CONSTRAINTS.yaml` | Правило `ML-PLAYBOOK-1` типа `playbook_graduation` | да (fitness) |
| `CONSTRAINTS.yaml` / пресет `aiml/presets/ml-researcher/config.toml` | Комментарий: `[knowledge] dirs` должен включать каталог `…/distillate` (иначе записанный playbook не виден `kb_search`) | нет (документация) |
| `docs/control.md` | Раздел про `playbook_graduation` рядом с `skill_contract` | нет (документация) |
| `aiml/plugins/aiml-ops/skills/knowledge-routing/SKILL.md` | Не трогать — уже описывает playbook и градацию; сверить формулировки | нет |

`src/config.rs` — **не трогаем**: новых настроек нет, `KnowledgeConfig` уже
покрывает каталоги; порог `PLAYBOOK_USES_MIN` — константа кода (как
`MIN_CONTENT_CHARS`), не конфиг.

---

## 10. Тесты

### 10.1. `src/distill.rs` (unit; провайдеры-заглушки рядом с `MdLlm`)

Добавить `PlaybookLlm` (возвращает
`"<!-- routing: playbook -->\n---\ntype: playbook\nstatus: draft\nskill_candidate: true\n---\n\n# Процедура\n\n## Когда применять\n…\n\n## Методика\n1. …\n2. …\n3. …\n"`).

| # | Тест | Проверка |
|---|---|---|
| 1 | `distill_routes_playbook_to_playbooks_dir` | путь оканчивается `arch-distilled/distillate/playbooks/<slug>.md` |
| 2 | `distill_playbook_stamps_type_and_uses` | паспорт содержит `type: playbook`, `uses: 1`, `skill_candidate: true` |
| 3 | `distill_playbook_stamps_steps_hash` | при непустой «Методике» есть `steps_sha256:`, совпадает с `steps_hash(body)` |
| 4 | `distill_playbook_forces_draft` | `status: stable` из ответа модели → в файле `draft` |
| 5 | `bump_playbook_uses_increments_same_steps` | два bump'а при том же теле → `uses` 1→2→3 |
| 6 | `bump_playbook_uses_resets_on_step_change` | bump, правка «Методики», bump → `uses == 1`, хэш перезакреплён |
| 7 | `bump_playbook_uses_errors_without_metodika` | тело без «Методики» → `HarnessError::Tool`, файл не изменён |
| 8 | `graduate_refuses_below_three_uses` | `uses: 2` → `HarnessError::Control`, `skills/` пуст |
| 9 | `graduate_refuses_missing_candidate` | `skill_candidate: false` → отказ |
| 10 | `graduate_refuses_changed_steps` | `steps_sha256` ≠ текущему хэшу → отказ |
| 11 | `graduate_writes_skill_and_marks_graduated` | `SKILL.md` записан; playbook: `status: graduated`, `graduated_to:` указывает на него, баннер `[graduated]`, файл на месте |
| 12 | `graduate_is_not_idempotent_silently` | повторный `graduate` → отказ (уже graduated), скилл не перезаписан |
| 13 | `graduate_protects_foreign_plugin` | чужой plugin → `HarnessError::Tool`, отказ «не затираю» |
| 14 | `graduate_rejects_non_skill_shape` | методика из 1 шага → `Control` (контракт скилла) |
| 15 | `parse_route_playbook_marker` | через `PlaybookLlm`; отдельно — маркер `<!-- routing: playbook -->` не попадает в файл |

Существующие тесты (`distill_routes_md_to_distillate`, `…_rejects_non_skill_artifact`)
правятся под 4-вариантный enum — по-прежнему зелёные.

### 10.2. `src/control.rs` (unit, рядом с тестами `skill_contract_violations`, стр. 5762-5805)

| # | Тест | Проверка |
|---|---|---|
| 16 | `playbook_graduation_accepts_ripe` | `type: playbook` + `uses: 3` + candidate + hash + методика ≥3 → `[]` |
| 17 | `playbook_graduation_flags_thin_uses` | `uses: 2` → нарушение про порог |
| 18 | `playbook_graduation_flags_no_candidate` | `skill_candidate: false` → нарушение |
| 19 | `playbook_graduation_flags_missing_hash` | нет `steps_sha256` → нарушение «журнала нет» |
| 20 | `playbook_graduation_flags_thin_metodika` | методика из 1 шага → нарушение |
| 21 | `playbook_graduation_flags_wrong_type` | `type: digest` → нарушение |
| 22 | `playbook_graduation_rule_reports_on_repo` | tempdir-репо с `graduated_to` в никуда → находка через `check` |

### 10.3. `assets/rubrics/playbook_graduation.yaml`

Загрузка через `rubric::load` в тесте (`rubric.rs:233`) — YAML парсится, критерии
на месте (общий тест «все рубрики каталога загружаются» уже есть; новый файл его
удовлетворяет).

### 10.4. Гейты

- `cargo check` и `cargo test` — зелёные (полный прогон).
- `cargo clippy` без новых предупреждений; новых `unwrap`/`expect` вне
  `#[cfg(test)]` нет; ошибки — `HarnessError`/`Result`, сбой инструмента —
  `ToolOutput::err`; doc-комментарии — русские, код — английский.
- `skill_contract_violations` и существующие скиллы не сломаны (тесты control.rs).
- `aiml/plugins/aiml-ops/skills/knowledge-routing/SKILL.md` проходит гейт
  (формулировки playbook из этого спека ей не противоречат).

---

## 11. Что НЕ трогаем (границы волны)

| Не меняем | Почему |
|---|---|
| `ensure_frontmatter` для скиллов | контракт скилла как был; playbook-паспорт — отдельная функция `stamp_playbook` |
| `skill_contract_violations` | та же функция валидирует репозиторий (control.rs:5806); требование playbook-полей сломало бы скан скиллов |
| `frontmatter.rs` (модуль из смежного спека) | ещё не реализован; этот спек обходится `frontmatter_field` + локальным `upsert_fm_field`. При появлении модуля — переезд хелперов без смены поведения |
| Ветка `Md` | остаётся плоским `distillate/<slug>.md`; `type: playbook` из тела `Md` — забота frontmatter-спека |
| `MIN_CONTENT_CHARS` / `MAX_CONTENT_CHARS` | те же лимиты для всех маршрутов |
| Порог `uses` как настройка конфига | `PLAYBOOK_USES_MIN` — константа (правило трёх повторений фиксировано методологией); параметризация — при реальной нужде |
| Удаление playbook | только `[graduated]`-пометка; удаление — никогда в этой волне |

---

## 12. Краевые случаи и риски

| Риск | Решение |
|---|---|
| Playbook-файл не под `[knowledge] dirs` | Запись идёт в `<plugins_root>/<plugin>/distillate/playbooks/`; чтобы `kb_search` его видел, каталог плагина должен быть в `KnowledgeConfig::dirs` (в `ml-researcher` пресете — `~/library/distillate`, `~/knowledge/ml`). Отметить в доке/пресете, не в коде |
| Модель вернула `routing: md` для процедуры | Playbook не создан, но паспорт `type: playbook` подставит frontmatter-спек по телу с `uses:`. Обе ветки дают согласованный артефакт |
| Модель сама написала `steps_sha256` / `status: stable` | Хэш пересчитывается харнессом (модельный игнор); `stable`/`graduated` форсятся в `draft` |
| Правка методики после двух применений | `bump` обнуляет `uses` до 1 — правило трёх повторений перезапускается (это и есть цель) |
| «Методика» раздута/многострочна | `methodology_body` режет по следующему заголовку; хэш стабилен (нормализация хвостовых пробелов/CRLF перед хэшем) |
| `graduated_to` в никуда | Fitness-правило §5.2 п.1 ловит |
| Playbook-пустой `skills/` после отказа градации | Порядок §6.1: проверка → запись скилла → архив. Отказ оставляет playbook нетронутым, `skills/` не создаётся |
| Повторная градация | Предикат §5.1 п.6 отклоняет; скилл не перезаписывается |
| Раздутый playbook (> 5 МБ) | `kb_search` не читает тело (`MAX_CONTENT_BYTES`), но файл существует; градация работает по прямому чтению (не через kb). Прежнее поведение kb — зафиксировать в доке |

---

## 13. Порядок работ и DoD

1. **`src/control.rs`** — `PLAYBOOK_USES_MIN`,
   `playbook_graduation_violations`, `frontmatter_field` → `pub(crate)`; тесты 16-21.
2. **`src/distill.rs`** — `DistillRoute::Playbook` + `as_str` + `parse_route`;
   `methodology_body`/`steps_hash`/`stamp_playbook`/`upsert_fm_field`;
   ветка в `distill_to_skill`; `bump_playbook_uses`; тесты 1-7.
3. **`src/distill.rs`** — `graduate_playbook` + `PlaybookGraduateTool`; регистрация
   в `tools`; глагол вывода `skill_distill`; тесты 8-15.
4. **`assets/prompts/skill_distiller.md`** — ветка `playbook` + паспорт + правила (§7).
5. **`assets/rubrics/playbook_graduation.yaml`** — судейская рубрика (§5.3).
6. **`src/agent/slash.rs`** — `/playbook list|use|graduate`.
7. **`CONSTRAINTS.yaml`** — `RuleKind::PlaybookGraduation` + правило `ML-PLAYBOOK-1`;
   тест 22 и репо-скан.
8. `cargo check` + `cargo test` + `clippy`; тесты `skill_contract_violations`,
   `concept.rs` — зелёные.

**Готово, когда:**

- `/distill`/`skill_distill` умеют маршрут `playbook` и пишут
  `distillate/playbooks/<slug>.md` с валидным паспортом
  (`type: playbook`, `uses: 1`, `skill_candidate: true`, `steps_sha256`,
  `status: draft`);
- применения инкрементируют `uses` при неизменной методике и обнуляют счётчик при
  её изменении (журнал стабильности работает);
- `graduate_playbook` **отказывается** писать в `skills/`, пока `uses < 3`, нет
  `skill_candidate`, нет журнала или шаги менялись; при выполнении — пишет
  `SKILL.md` через контракт скилла и помечает playbook `[graduated]` с
  `graduated_to` (файл не удаляется);
- судейская рубрика `playbook_graduation.yaml` описывает семантику (повторяемость,
  устойчивость, обобщаемость, форма) — критерий-суждение не уехал в код/хук;
- fitness-правило `playbook_graduation` ловит `graduated_to` в никуда и градацию
  ниже порога;
- новых крейтов нет, все тесты (новые и существующие) зелёные.
