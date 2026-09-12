# P1-спека: frontmatter-фасеты в `kb_search`

- Статус: **спека (не код)**. Дата: 2026-09-11. Владелец зоны: `src/kb.rs` (агент `web`), `src/distill.rs` (агент `tools`).
- Источник правил: `~/library/distillate/1_методология/концепт-фронтматтер-в-markdown.md`
  (паспорт документа: `type` → маршрутизация, `status: draft|stable` → гейт качества,
  `source`/`created` → provenance, `concepts[]` → граф, «поиск без чтения»).
- Мост: `aiml/notes/library-bridge.md`, строка **P1 «Frontmatter как фасет поиска»**.
- Инварианты: без `unsafe`; без `unwrap/expect` вне тестов; ошибки — `HarnessError`/`Result`;
  сбой инструмента — `ToolOutput::err`; **новых зависимостей нет** — `serde_yaml_ng = "0.10"`
  уже в `Cargo.toml`, `chrono` (feature `clock`) тоже.

---

## 1. Что теряется сегодня (аудит `src/kb.rs`)

| Место | Строки | Поведение | Дефект |
|---|---|---|---|
| `parse_file` | 463–504 | читает файл, режет на `LineIndex` (start/end/tokens), собирает `headings`, считает `doc_tokens` | frontmatter — обычные строки: `---`, `type: digest` токенизируются в `["type","digest"]` и **участвуют в BM25 как тело** |
| `tokenize` | 522–527 | `split(|c| !c.is_alphanumeric())` | `source: arXiv:2501.12345` → `["source","arxiv","2501","12345"]` — provenance попадает в контентный индекс как мусорные токены |
| `heading_title` | 507–519 | `#`-заголовки | `---` не заголовок, но и не отсечён — снипет иногда показывает паспорт как «текст» |
| `doc_stats` | 600–644 | tf по всем `file.lines` | метаданные раздувают `doc_tokens`, искажая BM25-нормализацию длины |
| `CachedFile` | 167–187 | `content/lines/headings/doc_tokens` | **нет поля метаданных** — YAML не парсится вообще |
| `SearchOptions` | 92–103 | `path_filter/extensions/names_only` | фильтра по `type`/`status`/`concepts` нет |
| `KbSearchArgs` | 952–965 | 4 аргумента | инструмент не умеет «только digest», «только stable» |
| `distill.rs::ensure_frontmatter` | 250–269 | синтезирует только `name`+`description` | артефакт не несёт `type`/`status`/`source`/`concepts` → фасет непригоден |
| `distill.rs` маршрут `Md` | 149–155 | пишет `payload + "\n"` | md-дистиллят **вообще без frontmatter** |

Итог: корпус библиотеки («карточки» и «дайджесты») уже размечен машинно-читаемым
паспортом (`type: digest`, `status: draft`, `source: arXiv:…`, `concepts: [kebab-case]`),
но харнесс его **слеп** (`frontmatter-blind`). Метаданные работают как шум в BM25
вместо дешёвого O(1)-роутинга.

---

## 2. Модель данных: `FrontmatterFacets`

Новый тип в `src/kb.rs` (рядом с `KbHit`, до `CachedFile`). Реквизиты — из методологии,
сознательно **минимальный набор P1**; `uses`/`skill_candidate` зарезервированы под P1-playbook,
в парсер входят сразу (иначе второй проход правки).

```rust
/// Паспорт документа из YAML-frontmatter (см. концепт-фронтматтер-в-markdown).
/// Все поля — сырые строки: kb не является валидатором библиотеки, он фасетирует.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct FrontmatterFacets {
    /// Класс документа: concept | digest | synthesis | playbook | skill.
    pub r#type: Option<String>,
    /// Гейт качества: draft (агент) | stable (ревью).
    pub status: Option<String>,
    /// Provenance: `arXiv:2501.12345`, `диалог 2026-08-04`.
    pub source: Option<String>,
    /// Теги-концепты (kebab-case, 3–7).
    #[serde(default)]
    pub concepts: Vec<String>,
    /// Дата создания `YYYY-MM-DD` (строка: терпим к частичным датам).
    pub created: Option<String>,
    /// Число применений playbook (P1, зарезервировано).
    pub uses: Option<u32>,
    /// Пометка «кандидат в скилл» (P1, зарезервировано).
    pub skill_candidate: Option<bool>,
}

impl FrontmatterFacets {
    /// Паспорта нет вовсе (файл без `---`).
    pub fn is_empty(&self) -> bool { /* все None, concepts пуст */ }
}
```

Решения:

- **Не переиспользуем `model::Frontmatter`** (`src/model/parse.rs:157`) и `model::split_frontmatter`:
  у модели `type` — это enum сущности (`cap`/`adr`/…), а у нас свободный класс документа;
  `kb` работает по «диким» md из `knowledge.dirs`, где frontmatter может быть битым — нужен
  терпимый разбор, а не `Result`-строгий. Связывать kb с моделью (entity-контур `model/`) нельзя.
- **Ключевое имя `type`** — serde: `#[serde(rename = "type")]` не нужен, поле так и названо;
  используем `r#type` либо `#[serde(rename = "type")] kind: Option<String>` по стилю `model.rs:161`.
  Предпочтение — второй вариант (raw-идентификатор читается хуже в шаблонах).
- `#[serde(default)]` на всех `Option`-полях, чтобы `concepts: [a,b]` (flow-sequence) и одиночная
  строка `concepts: dpo` разбирались одинаково (`serde_yaml_ng` примет обе формы в `Vec<String>`).

---

## 3. Парсинг при индексации (`src/kb.rs`)

### 3.1 Новые поля `CachedFile` (после `headings`, стр. 182)

```rust
/// Разобранный паспорт (None — frontmatter отсутствует/битый; паспорт-маркер `---` есть, но YAML не разобран — тоже None).
frontmatter: Option<FrontmatterFacets>,
/// Индекс строки, с которой начинается тело (0 — frontmatter нет). Строки `< body_start`
/// исключаются из контентного скоринга (паспорт — не тело).
body_start: usize,
```

`name_only()` (стр. 191–202) дополняется `frontmatter: None, body_start: 0`.

### 3.2 Новая функция `scan_frontmatter` (рядом с `heading_title`, стр. ~507)

Двухуровневый, терпимый разбор — **не** `Result`:

```rust
/// Разбирает YAML-frontmatter в начале текста. Возвращает (facets, body_start_line).
/// Терпимый контракт: битый YAML не ошибка — фасетов нет, но `body_start` всё равно
/// указывает за закрывающий `---`, чтобы паспорт не тек в контентный скоринг.
fn scan_frontmatter(text: &str) -> (Option<FrontmatterFacets>, usize) {
    // 1. BOM + `---` первой значимой строкой (строгие парсеры: Obsidian/Jekyll).
    //    Допускаем один ведущий HTML-комментарий `<!-- filename: … -->`
    //    (методология: «комментарий → frontmatter» для агентов безразличен).
    // 2. Ищем закрывающий `---` (строка целиком, не `----`).
    // 3. serde_yaml_ng::from_str::<FrontmatterFacets>(yaml):
    //      Ok  → нормализация (trim, lowercase type/status/concepts),
    //      Err → построчный фолбэк scan_frontmatter_scalars:
    //            ключи type/status/source/created (значение до конца строки, снять кавычки)
    //            и concepts: `[a, b]` | `a, b` | многострочный `- a`.
    (facets, body_start)
}
```

Почему фолбэк обязателен: модель (и люди) пишут `description: Текст: с двоеточием` —
это невалидный YAML (`mapping values are not allowed here`). `plugin.rs::parse_frontmatter`
(стр. 200–249) уже решает эту же проблему построчным сканом; в kb повторяем приём локально,
но извлекаем только нужные фасеты. Нормализация: `type`/`status` → `trim().to_lowercase()`,
`concepts` → `trim().to_lowercase()` по каждому тегу.

### 3.3 Правка `parse_file` (стр. 463–504)

```rust
let (frontmatter, body_start) = if markdown { scan_frontmatter(&text) } else { (None, 0) };
// ...
for (idx, raw) in text.split_inclusive('\n').enumerate() {
    let line = raw.trim_end_matches(['\n', '\r']);
    let tokens = tokenize(line);
    // Паспорт — не тело: строки frontmatter не идут ни в doc_tokens, ни в токены строк.
    if idx < body_start {
        lines.push(LineIndex { start: offset, end: offset + line.len(), tokens: Vec::new() });
        offset += raw.len();
        continue;
    }
    doc_tokens += tokens.len();
    if let Some(title) = heading_title(line) { headings.push((idx, title)); }
    lines.push(LineIndex { start: offset, end: offset + line.len(), tokens });
    offset += raw.len();
}
```

Важно: `lines` по-прежнему содержит **все** строки с точными байт-смещениями — `make_hit`
(стр. 848–857) и `breadcrumb_at` (890–899) индексируют `lines` по номеру строки, ломать
соответствие «индекс ↔ байтовый диапазон» нельзя. Пустые токены у паспортных строк дают
нулевой вклад в `line_masks` — они не могут стать «лучшей строкой» хита.

Следствие: `doc_tokens` и `avgdl` считаются по телу без паспорта — BM25 перестаёт искажаться
на 6–10 строках метаданных, что заметнее на коротких карточках (300–800 токенов).

### 3.4 Префилт по фасетам в `search_blocking` (после `evict_if_needed`, стр. 282)

```rust
// Фасетный фильтр — до скоринга: df/n_docs/avgdl должны считаться по суженому корпусу,
// иначе idf «размывается» документами, которые всё равно отфильтруются.
let filtered: Vec<PathBuf> = if options.facets.is_empty() {
    candidates
} else {
    candidates
        .into_iter()
        .filter(|p| corpus.files.get(p).is_some_and(|f| f.facets_match(&options.facets)))
        .collect()
};
let mut hits = score_corpus(corpus, &filtered, &terms, options, MatchMode::Stem);
if hits.is_empty() {
    hits = score_corpus(corpus, &filtered, &terms, options, MatchMode::Fuzzy);
}
```

Один префилт на оба прохода — иначе `Stem` и `Fuzzy` увидят разные корпуса.

---

## 4. Фильтр в `kb_search` (аргументы инструмента)

### 4.1 `SearchOptions` (стр. 92–103) — добавить поле

```rust
/// Фасеты frontmatter: пустой — без фильтра. Семантика — §4.4.
pub facets: FacetFilter,
```

### 4.2 `FacetFilter` (новый тип, там же)

```rust
/// Фильтр по паспортам. Внутри поля — OR, между полями — AND.
/// Пустой список = поле не ограничивает. Файл без frontmatter не проходит
/// ни один непустой фильтр (строгий режим — фильтр означает «только размеченные»).
#[derive(Debug, Clone, Default)]
pub struct FacetFilter {
    pub types: Vec<String>,      // type ∈ ...
    pub statuses: Vec<String>,   // status ∈ ...
    pub concepts: Vec<String>,   // concepts ∩ ... ≠ ∅
}

impl FacetFilter {
    /// Пустой фильтр — ограничений нет.
    pub fn is_empty(&self) -> bool {
        self.types.is_empty() && self.statuses.is_empty() && self.concepts.is_empty()
    }
}
```

Матчинг — метод на `CachedFile` (владеет `facets`):

```rust
impl CachedFile {
    /// Проходит ли файл фасетный фильтр (см. FacetFilter: OR внутри, AND между).
    fn facets_match(&self, f: &FacetFilter) -> bool {
        let Some(fm) = &self.frontmatter else { return f.is_empty(); };
        let type_ok = f.types.is_empty()
            || fm.r#type.as_deref().is_some_and(|t| f.types.iter().any(|x| x == t));
        let status_ok = f.statuses.is_empty()
            || fm.status.as_deref().is_some_and(|s| f.statuses.iter().any(|x| x == s));
        let concepts_ok = f.concepts.is_empty()
            || f.concepts.iter().any(|c| fm.concepts.iter().any(|x| x == c));
        type_ok && status_ok && concepts_ok
    }
}
```

### 4.3 `KbSearchArgs` (стр. 952–965) — новые поля

```rust
/// Фильтр по `type` паспорта: строка или массив (digest | concept | playbook | synthesis | skill).
#[serde(default, deserialize_with = "string_or_seq")]
filter_type: Vec<String>,
/// Фильтр по `status`: `draft` (написано агентом) | `stable` (прошло ревью).
#[serde(default, deserialize_with = "string_or_seq")]
filter_status: Vec<String>,
/// Фильтр по `concepts`: документ имеет хотя бы один из перечисленных тегов.
#[serde(default, deserialize_with = "string_or_seq")]
filter_concepts: Vec<String>,
```

`string_or_seq` — маленький хелпер (`Option<String> | Vec<String> → Vec<String>`, нормализация
lowercase/trim/дедуп), чтобы агент мог передать `"filter_type": "digest"` и `["digest","playbook"]`
одинаково. Имена в схеме — с префиксом `filter_`, чтобы не конфликтовать с `extensions`/`path_filter`
и явно читаться как ограничение.

В `call` (стр. 990–1000):

```rust
if !options.facets.is_empty() && options.names_only {
    return Ok(ToolOutput::err(
        "kb_search: фильтр по фасетам требует чтения содержимого — несовместим с names_only=true",
    ));
}
```

Причина: `names_only` даёт `CachedFile::name_only` без контента и без паспорта → фасетный фильтр
вернул бы 0 хитов всегда. Это гарантированная ошибка вызова, а не пустой результат.

### 4.4 Семантика фильтра (нормативно)

1. Поле не задано → не ограничивает.
2. `filter_type: [digest, playbook]` → `type ∈ {digest, playbook}`.
3. `filter_type: [digest]` + `filter_status: [stable]` → `type = digest AND status = stable`.
4. `filter_concepts: [dpo, grpo]` → у документа есть `dpo` **или** `grpo`.
5. Файл без frontmatter / с битым YAML → не проходит любой непустой фильтр.
6. Сравнение — точное по нормализованным значениям (lowercase/trim). Регистр в файле и в запросе
   приводится к нижнему; `Type: Digest` и `type: digest` эквивалентны.
7. Файл ≥ `MAX_CONTENT_BYTES` (5 МБ) индексируется по имени без контента → паспорта нет → под
   фильтр не попадает (осознанно: гигантские файлы вне фасетного контура).
8. Фильтр сужает корпус **до** BM25-статистик (idf/avgdl) — как в §3.4.

### 4.5 Выдача `KbHit` и текст инструмента

Добавить в `KbHit` (стр. 75–90) поле-паспорт (не ломает сериализацию — `#[serde(default)]`):

```rust
/// Паспорт файла (пусто — frontmatter отсутствует).
#[serde(default, skip_serializing_if = "Option::is_none")]
pub facets: Option<FrontmatterFacets>,
```

Заполняется в `make_hit` (858) и в ветке «по имени» (785) из `file.frontmatter`.

Вывод инструмента (`call`, стр. 1007–1025) — строка-паспорт после заголовка хита:

```
── ~/library/distillate/2_статьи/grpo.md:12 (score 41.3) › Метод
   [type=digest · status=⚠ draft · concepts=grpo,reward]
```

Правило: `status: stable` печатать как `stable`, `draft` — как `⚠ draft (не ревью)`,
`None` — не печатать поле. Так «сырьё агента» видно прямо в выдаче, без второго вызова.

### 4.6 Описание инструмента

В `description` (стр. 975) добавить в перечисление сужений:
«…сузить по паспорту — `filter_type: "digest"`, `filter_status: "stable"` (только прошедшее
ревью), `filter_concepts: ["dpo"]`; комбинируются логическим И».

### 4.7 Потребители

`KbHit`/`SearchOptions` используются в `src/main.rs:1092`, `src/agent/slash.rs:533`,
`src/mcp_server.rs:586`. Новое поле `SearchOptions` — `#[derive(Default)]`, поэтому вызовы
`SearchOptions::default()` не ломаются; MCP-обёртка (`mcp_server.rs:571`) наследует схему
аргументов из `KbSearchTool::spec` автоматически (проверить, что она не дублирует схему руками).

---

## 5. `status: draft|stable` как маркер «агент vs ревью»

- `draft` — файл порождён агентом/конвейером, ревью не проходил. `stable` — поднят человеком.
- kb **не переписывает** статусы и **не понижает** их неявно: статус — факт корпуса, а не вывод kb.
- Видимость: `filter_status` + печать `⚠ draft` в выдаче (§4.5). Это даёт модели в один вызов
  понять, можно ли опираться на факт как на устоявшийся (`stable`) или перед ней сырьё (`draft`).
- **Мягкое понижение веса (опционально, выключено по умолчанию).** Резерв: константа
  `DRAFT_SCORE_WEIGHT: f64 = 1.0`; при значении `< 1.0` хит с `status: draft` умножается на неё
  в `score_corpus` перед сортировкой. Вводить в P1 **не** включаем: неявное ранжирование хуже
  предсказуемого фильтра. Оставить как точку расширения с явным конфиг-ключом.
- Дефолт выдачи — без фильтра (видны оба статуса), чтобы не терять материал молча.

---

## 6. Эмиссия валидного frontmatter в `distill.rs`

Требование (г): артефакт, порождённый `skill_distill`//`distill`, должен нести паспорт,
пригодный для фасетного поиска, и **не объявлять себя `stable`**.

### 6.1 Замена `ensure_frontmatter` (стр. 250–269) на `normalize_frontmatter`

```rust
/// Гарантирует валидный паспорт артефакта: снимает код-фенсы модели, при необходимости
/// достраивает `---`-блок и инжектит недостающие фасеты. Инвариант: агент пишет только
/// `status: draft`; `stable` присваивает человек/команда градации, и перезапись его сохраняет.
fn normalize_frontmatter(
    raw: &str,
    slug: &str,
    route: DistillRoute,
    source: Option<&str>,
) -> String
```

Алгоритм:

1. Снять код-фенс (` ``` ` / ` ```markdown `), как сейчас.
2. Если `---`-блок есть — распарсить `scan_frontmatter`-эквивалентом (терпимо), взять `type`,
   `status`, `source`, `concepts`, `created`; если нет — синтезировать (описание — первая
   непустая не-`#` строка, ≤280 символов, как сейчас).
3. Инжект недостающего:
   - `type`: из `route` — `Skill → skill`, `Md → playbook`; если модель уже задала `type: digest|concept|synthesis` в md-маршруте, **уважать** его (декларативная справка ≠ playbook).
   - `status`: если файла ещё нет — `draft`. Если файл существует и в нём был `stable` — сохранить `stable` (агент не понижает прошедшее ревью).
   - `source`: аргумент `source`; нет аргумента — `source: ""` не писать вовсе (пустое поле хуже отсутствия). Деривация из первой строки тела — **не** делать (мусорный provenance вреднее пустого).
   - `concepts`: только то, что вернула модель; `[]` не писать.
   - `created`: если файла нет — `chrono::Utc::now().format("%Y-%m-%d")`; если есть — сохранить прежнюю дату из старого паспорта.
4. Порядок ключей — фиксированный (`name`, `description`, `type`, `status`, `source`, `concepts`, `created`) для диффопригодности.
5. Значения-строки не оборачивать кавычками, если в них нет `:`/`#`/ведущего спецсимвола; при наличии — взять в двойные кавычки (валидный YAML без сюрпризов).

### 6.2 Маршрут `Md` (стр. 149–155)

Сейчас пишет `payload + "\n"` **без** паспорта. Заменить на
`format!("{}\n", normalize_frontmatter(&payload, &slug, DistillRoute::Md, source))`.
Так playbook/справка попадают в корпус фасетируемыми (`type: playbook`, `status: draft`),
что и есть «поиск без чтения» из методологии.

### 6.3 Промпт `assets/prompts/skill_distiller.md`

- В блок формата результата для `routing: skill` добавить после `description` строки паспорта:
  `type: skill`, `status: draft`, `source: <provenance или пусто>`, `concepts: [<3–7 kebab-case>]`.
- Для `routing: md` — явно указать `type: playbook | concept | digest` в первой строке паспорта.
- Правило в тексте промпта: **никогда не ставить `status: stable`** — это решение человека
  после ревью; агент всегда пишет `draft`.
- Ограничение: правку промпта валидировать тестом §7.8 (в паспорте записанного артефакта
  есть `type` и `status`, `status != stable`).

### 6.4 Контракт `control::skill_contract_violations` (стр. 2568) — не трогать в P1

Расширять его проверкой `type`/`status` **нельзя без отдельного решения**: это ужесточит
гейт для существующих плагинных скиллов, у которых каталог паспортов иной. Фасетную
нормализацию держим в `distill.rs` (зона записи), контракт скилла оставляем как есть.

### 6.5 Сигнатура и совместимость

`distill_to_skill` (стр. 96) получает новый параметр `source: Option<&str>`
(или `DistillArgs`-структуру). Вызовы — `src/agent/slash.rs` (`/distill`) и `SkillDistillTool::call`
(стр. 316): у инструмента добавить необязательное поле `source` в схему (стр. 295–312) —
provenance (arXiv-id, URL, имя сессии). Отсутствие `source` не ошибка.

---

## 7. План тестов (все — `#[cfg(test)]` в `kb.rs`/`distill.rs`, временные каталоги `tempfile`)

**Парсер (`kb.rs`)**
1. `scan_frontmatter_parses_facets` — `---\ntype: digest\nstatus: draft\nsource: arXiv:2501.1\nconcepts: [dpo, grpo]\ncreated: 2026-08-04\n---\n` → все поля, `concepts == ["dpo","grpo"]`, `body_start` = строка после закрывающего `---`.
2. `scan_frontmatter_tolerates_colon_in_description` — `description: Текст: с двоеточием` → скалярный фолбэк; `type`/`status` извлечены, ошибки нет.
3. `scan_frontmatter_accepts_leading_html_comment` — `<!-- filename: x.md -->\n---\ntype: concept\n---` → паспорт найден (методология: порядок «комментарий → frontmatter» допустим для агентов).
4. `scan_frontmatter_absent_and_broken_are_none` — файл без `---` и файл с незакрытым `---` → `(None, …)`, паника отсутствует.
5. `frontmatter_lines_excluded_from_body_scoring` — корпус с `type: digest` в паспорте; запрос `type` **не** даёт контентного хита (нулевые токены паспортных строк).

**Фильтр (`kb.rs`)**
6. `filter_type_and_status_narrows_corpus` — 3 файла (digest/draft, concept/stable, без паспорта); `filter_type=[digest]` → 1 хит; `filter_status=[stable]` → 1; без фильтра → все совпадения по телу.
7. `filter_within_field_is_or_between_fields_is_and` — `filter_type=[digest, concept]` → 2; `+ filter_status=[stable]` → 1 (только concept/stable).
8. `filter_concepts_matches_any_tag` — `concepts: [a, b]`, `filter_concepts=["b"]` → хит.
9. `filter_excludes_docs_without_frontmatter` — файл без паспорта не попадает ни в один непустой фильтр.
10. `filter_is_case_insensitive` — `Type: Digest` в файле, `filter_type: "digest"` → хит.
11. `filter_conflicts_with_names_only` — `names_only=true` + `filter_type=[digest]` → `ToolOutput::err` (сообщение содержит «names_only»).
12. `filter_corpus_affects_idf` — контроль: сужение корпуса меняет `avgdl`/idf так, что хит релевантнее, чем в полном корпусе (защита от «фильтр после скоринга»).

**Эмиссия (`distill.rs`)**
13. `normalize_frontmatter_injects_required_facets` — `Skill`-маршрут, модель вернула паспорт без `status` → запись содержит `type: skill`, `status: draft`, `created`.
14. `normalize_frontmatter_never_emits_stable` — модель вернула `status: stable` при новом файле → на диске `draft` (агент не объявляет ревью).
15. `normalize_frontmatter_preserves_existing_stable_and_created` — файл существует со `stable`/`created: 2026-01-01` → перезапись сохраняет оба.
16. `md_route_writes_frontmatter` — `routing: md` → файл в `distillate/` начинается с `---`, содержит `type: playbook` и `status: draft`.
17. `distilled_skill_is_searchable_by_facet` — сквозной: `distill_to_skill` пишет артефакт в `plugins_root`, тот же каталог в `knowledge.dirs`, `kb_search` с `filter_status=[draft]` его находит.

**Совместимость**
18. `kb_hit_serde_defaults_without_facets` — JSON без `facets` десериализуется (`#[serde(default)]`), round-trip сохраняет паспорт.
19. `search_without_facets_unchanged` — существующие тесты `kb.rs` (ранжирование, сниппет, кэш) остаются зелёными без правок: фильтр по умолчанию пуст.

---

## 8. Изменения по файлам

| Файл | Что |
|---|---|
| `src/kb.rs` | `FrontmatterFacets`, `FacetFilter`, `scan_frontmatter` (+скан-фолбэк), поля `CachedFile.{frontmatter,body_start}`, правка `parse_file`/`name_only`, префилт в `search_blocking`, поле `SearchOptions.facets`, поле `KbHit.facets`, `facets_match`, аргументы и вывод `KbSearchTool`, хелпер `string_or_seq`, тесты §7.1–7.12, 7.18–7.19 |
| `src/distill.rs` | `ensure_frontmatter` → `normalize_frontmatter` (route+source+created+status-инвариант), md-маршрут с паспортом, аргумент `source` в инструменте и сигнатуре `distill_to_skill`, тесты §7.13–7.17 |
| `assets/prompts/skill_distiller.md` | паспорт в формате ответа для `skill` и `md`; правило «агент пишет только draft» |
| `aiml/presets/ml-researcher/config.toml` | `[knowledge] dirs` на доменные каталоги библиотеки (P0-строка моста) — чтобы фасеты было на чём искать; без этого спека не проверяется на реальном корпусе |
| `docs/plugins_and_skills.md` / `README.md` | абзац про фасеты `kb_search` (по мере правки) |

**Не добавлять зависимостей:** YAML — `serde_yaml_ng` (есть), дата — `chrono` (есть).

---

## 9. Риски и краевые случаи

- **Дублированный frontmatter** в карточках библиотеки (`library/concepts/<type>/<slug>.md` —
  «фронтматтер часто дублирован, брать последний блок», мост §Источники). Правило: `scan_frontmatter`
  берёт **первый** блок от начала файла; если за телом встречен второй `---`-блок, он остаётся телом
  (в контентном индексе). Пометка на будущее: карточки `concept_search` (P0) парсят своим правилом.
- **`concepts` многострочной формой** (`concepts:\n  - a\n  - b`) — покрывается `serde_yaml_ng`;
  скалярный фолбэк собирает indented `- `-строки до первого не-indented ключа.
- **Регистр/пробелы** — нормализация на входе индексации; сравнение точное по нормализованному.
- **`type` ≠ enum** — kb намеренно не валидирует словарь классов (библиотека растёт); неизвестный
  `type` фасетируется как есть, фильтр работает по точному значению.
- **Перф:** парсинг YAML — один раз на файл при индексации (кэш по `mtime+len`, `refresh_file`),
  не на каждый запрос; на горячем кэше стоимость нулевая. `serde_yaml_ng` на 200 символов паспорта — вне бюджета.
- **Обратная совместимость:** все новые поля — с дефолтами; пустой фильтр = прежнее поведение;
  `body_start=0` для не-md и файлов без паспорта = прежний скоринг.

## 10. Не-цели (вне P1-спеки)

- `DRAFT_SCORE_WEIGHT` (неявное понижение ранга) — только как точка расширения, выключено.
- Написание `stable`/градация playbook → SKILL (`uses ≥ 3`) — P1-playbook, отдельная спека.
- Фасеты `level`/`formality`/`family` карточек концептов — это `concept_search` (P0), не `kb.rs`.
- Леджер дедупликации (`skill_state.json`) и managed-блоки с якорными диффами — отдельные P1-строки моста.
