# СПЕК: доменный инструмент `concept_search` (база концептов Ариадны)

**Статус:** проект, к реализации (P0). **Дата:** 2026-09-11.
**Зона:** `aiml/` (толстый домен ML за типизированной границей; ядро остаётся
нейтральным — AD-1/AD-BE1). Механизм инструмента живёт в доменном модуле,
семантика ML-домена включается данными пресета/конфига.

**Связанные заметки:** `aiml/notes/library-bridge.md` (P0-строка — этот
инструмент), `aiml/notes/domain-ontology.md` §3 (`concept_search` для ML:
контракт «гипотеза со ссылкой»).

---

## 1. Точная JSON-схема индекса (проверено 2026-09-11)

### 1.1. `library/index/concept_index.json` — 126 746 166 байт

Порядок ключей в файле (важно для стратегии загрузки): `concept_index` →
`graph` → `stats`.

| Верхний ключ | Смещение | Объём | Тип |
|---|---|---|---|
| `concept_index.concepts` | 27 | ~61.2 МБ | object `<slug>` → карточка |
| `concept_index.aliases` | 61 223 819 | ~11.0 МБ | object `<alias>` → `<slug>` |
| `graph.nodes` | 72 178 493 | ~54.5 МБ | object `<slug>` → узел смежности |
| `graph.edges` | 126 745 821 (`[]`) | 0 | **пустой массив, всегда** (последний ключ блока `graph`) |
| `stats` | 126 745 840 | ~300 Б | сводка |

```jsonc
{
  "concept_index": {
    "concepts": {                       // 184 386 записей
      "<slug>": {
        "type": "architectural_component", // один из 6 (см. stats.by_type)
        "file": "concepts/architectural_component/0_5_model.md",
        "title": "π0.5 Model",            // string | null (138 null)
        "level": "β",                     // "α"|"β"|"γ" | null (10 616) | мусор
        "formality": "C",                 // "A"|"B"|"C" | null (10 670) | ""
        "family": "VLA Models",           // string | null (10 810) | "" (26 489; пустых всего 37 299)
        "maturity": null                  // сегодня ВСЕГДА null — не закладываться
      }
    },
    "aliases": { "<alias>": "<slug>" }    // 182 485; exact-match, регистрозначимо
  },
  "graph": {
    "nodes": { "<slug>": { "type": …, "title": …,
                           "edges_in":  ["<slug>", …],   // входящие рёбра
                           "edges_out": ["<slug>", …] } }, // 184 386 узлов
    "edges": []                            // ПУСТО: топология только в adjacency-полях
  },
  "stats": {
    "total_concepts": 186686,
    "by_type": { "architectural_component": 34827, "algorithmic_primitive": 44763,
                 "data_structure": 13343, "boundary_condition": 19514,
                 "procedural_step": 46157, "diagnostic_metric": 25782 },
    "rebuilt_at": "2026-09-10T11:37:14.009475"
  }
}
```

**Факты и ловушки (проверено):**

1. **`edges` всегда `[]`** — и в `concept_index.json`, и в `concept_graph.json`.
   Граф читается ТОЛЬКО из `nodes.*.edges_in/edges_out`. Кто ждёт `edges[]` —
   получит пустой граф и не заметит.
2. **`stats.total_concepts` (186 686) не совпадает с числом карточек** —
   `concepts` содержит **184 386** ключей (столько же, сколько узлов графа;
   множества slug'ов совпадают 1:1 — проверено `diff` ключей, 0 расхождений). `total_concepts`, по-видимому, считает
   md-файлы/записи извлечения с дублями. **Источник истины — `len(concepts)`;
   `stats` отдавать как «заявлено индексом» с пометкой расхождения.** Не
   строить на `stats` лимиты/навигацию.
3. **Карточка индекса НЕ содержит `sources` и `related`.** Они есть только в
   YAML-frontmatter md-файла (`library/concepts/<type>/<slug>.md`):
   `slug`, `type`, `level`, `formality`, `title`, `aliases`,
   `family`, `sources: [arxiv.2603.11101]`, иногда `related`. **Формат списков
   двойной**: и плоский `aliases: [a, b]`, и вложенный `aliases: [[a, b]]`
   (вложенный доминирует — 119 408 файлов), пустой = `[[]]`; `related` так же
   (`[]` 24 370, `[[]]` 23 950). Парсер обязан разворачивать на один уровень и
   принимать оба. **Значения `related` — не всегда slug'и**: встречаются
   ссылки-идентификаторы вида `C01`/`C1` (`related: [C01, C03]`) — их либо
   резолвить, либо честно отбрасывать, но не считать slug'ом. Frontmatter
   бывает продублирован в теле — брать последний блок (см. `library-bridge.md`).
   Ссылка на первоисточник (обязательна по ML-05/ML-11) берётся отсюда, значит
   нужен минимальный парсер frontmatter (P1) или чтение тела (P0).
4. **Данные грязные — фасеты нормализовать.** В `level` кроме α/β/γ встречаются
   `null` (10 616), `""` (3), `"advanced"`/`"core"`/`"meta"` (по 2–3).
   В `formality` — `null`/`""`. В `family` — 37 299 пустых и разнобой регистра
   («Evaluation Metrics» 1814 vs «evaluation metrics» 715). Фасет-фильтр:
   точное совпадение + регистронезависимый вариант; незнакомое значение —
   честный «0 совпадений», не ошибка.
5. **`type` — 6 известных значений** (см. `stats.by_type`); новых не
   закладывать, но и не паниковать на незнакомых — возвращать как есть.
6. **`file` — относительный путь от корня библиотеки**, `index_path.parent()`
   = `<lib>/index`, значит корень = `index_path.parent().parent()`. Все чтения
   тел/шаблонов — только после проверки, что канонизированный путь лежит под
   корнем (защита от `..`/абсолютных `file`).
7. **Файл отформатирован с отступами (2 пробела), а слаги бывают равны
   служебным словам.** В данных реально есть узел со slug `"edges"`
   (type `data_structure`), поэтому наивный `grep '"edges"'` даёт ложное
   смещение ~51.7 МБ — опираться на `jq`, не на grep. Настоящий `graph.edges` —
   последний ключ блока `graph` (байт 126 745 821), сразу перед `stats`.

### 1.2. `library/index/concept_graph.json` — 50 963 468 байт

Схема идентична блоку `graph` из `concept_index.json`: `{"nodes": {...},
"edges": []}`, 184 386 узлов, `edges` пуст. Узлы несут `type`/`title` (дублируют
карточки) + `edges_in`/`edges_out`. **Для смежности нужны только slug'и
соседей** — `type`/`title` игнорировать при разборе (экономия памяти).

Смежность (`edges_in`/`edges_out`) — это **имя-ссылки, не типизированные
рёбра**: направление и природу связи индекс не хранит (см. вопрос открытого
типа ребра в §8).

---

## 2. Существующий харнесс: точки интеграции (что и где)

| Файл | Что берём за образец |
|---|---|
| `src/tool.rs` | Трейт `Tool`: `fn spec(&self) -> ToolSpec`, `async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput>`, `fn timeout_secs(&self) -> u64` (дефолт 300 с). `ToolOutput::ok/err/truncated(max_chars)`. Реестр: `ToolRegistry::register/with/dispatch`; ошибка `call` превращается в `ToolOutput::err` — **не panic, не рвать ход**. |
| `src/tools.rs` | `fn domain_tools(cfg: &Config) -> Vec<Arc<dyn Tool>>` — сюда добавляется `out.extend(crate::concept::tools(cfg));`. `full_registry` навешивает политику автономии. Гейт инструмента — **на уровне регистрации** (эталон: `if cfg.web.enabled { out.extend(crate::web::tools()) }`). |
| `src/kb.rs` | `kb_search`: аргументы через `#[derive(Deserialize)] struct KbSearchArgs`, справка-описание в `ToolSpec::description` (русский, с примерами), тяжёлая работа — `tokio::task::spawn_blocking`, лимиты (`limit.min(20)`, `MAX_CONTENT_BYTES = 5 МБ`). Вывод — текст с `──`-разделителями. |
| `src/config.rs` | Секции конфига (`#[serde(default)] pub struct XConfig`), `Default` с нейтральными плейсхолдерами (никаких личных путей машины в коде — AGENTS), `expand_tildes()` (строки 1121–1140) — сюда добавить `concept.index`/`concept.graph`. `Config::home_dir()`. |
| `src/error.rs` | `HarnessError` (`Config`/`Io{path,source}`/`Json`/`Kb`…), `Result<T>`. **`HarnessError` не `Clone`** — кэш ошибок хранить строкой. |
| `src/main.rs` | `enum Cmd` (clap, `#[command(subcommand)]`), диспетчер `match cli.cmd { …, Some(Cmd::Model{cmd}) => cmd_model(cmd)?, … }`; `cmd_*`-функции на `anyhow`. |
| `src/distill.rs` | `pub fn tools(cfg: &Config) -> Vec<Arc<dyn Tool>>` домена — образец владения конфигом. |
| `docs/tools.md` | Таблица «Инструмент / Назначение / Параметры»; новая строка в доменном разделе. |
| `tests/cli.rs`, `tests/common/mod.rs` | Интеграционные тесты CLI через `assert_cmd` + изолированный дом (`common::arch_cmd`). |

Ограничение репозитория: `[lints.rust] unsafe_code = "forbid"` (Cargo.toml) —
**`unsafe` запрещён**, значит mmap без `memmap2` невозможен, а новых
зависимостей не добавляем (сеть может быть недоступна). Разрешено: std +
`serde`/`serde_json` (уже в зависимостях).

---

## 3. Стратегия загрузки 126 МБ

### 3.1. Выбор: (б) ленивый разовый разбор в процессе + мемоизация

Рассмотренные варианты:

- **(а) mmap + `from_slice` разово** — требует `memmap2` (новая зависимость) и
  обычно `unsafe`; при `unsafe_code = "forbid"` нереализуем без нового
  крейта. **Отклонено.**
- **(б) ленивый разбор при первом обращении, результат — в статическом кэше
  процесса.** Один разбор на процесс, дальше — обращения к готовым
  структурам. `std::fs::read` + `serde_json::from_slice`. **Выбрано.**
- **(в) компактный выгружаемый кэш при init** (свой бинарный/JSON-файл в
  `paths.state_dir`, штамп `{rebuilt_at,mtime,len}`) — быстрее холодный старт,
  но +формат, +сериализация, +инвалидация, +право записи. **Отложено в P1/P2**
  как оптимизация, если холодный старт окажется болезненным; совместимо с (б)
  без смены интерфейса.

### 3.2. Как именно грузим (важно)

Разбор идёт **в два независимых ленивых слоя**:

1. **Карточки+алиасы (`ConceptDb`)** — обязательный слой, грузится при первом
   любом вызове. Разбираем `concept_index.json` в структуру, которая
   **объявляет только `concept_index` и `stats`**; ключ `graph` (~54.5 МБ, 184 K
   узлов с векторами смежности) для `serde` — неизвестное поле, он
   пропускается через `IgnoredAny` (побайтовый скан без аллокаций). Итог:
   пик памяти ≈ буфер файла (127 МБ, дропается сразу после разбора) + карточки.
   Структуры после нормализации: `HashMap<Box<str>, Card>` (184 K) +
   `HashMap<Box<str>, Box<str>>` exact-алиасы (182 K) + свёрнутый
   lowercase-индекс алиасов + счётчики фасетов.
2. **Смежность (`AdjIndex`)** — грузится **только при первом `neighbors()`** из
   `concept_graph.json` (или из блока `graph` того же файла — но отдельный
   файл меньше и лежит рядом). Разбор в `HashMap<Box<str>, Adj>`, где
   `Adj { edges_in: Box<[Box<str>]>, edges_out: Box<[Box<str>]> }`;
   `type`/`title` узлов игнорируются. Внутри `ConceptDb` — свой
   `OnceLock<Result<Arc<AdjIndex>, String>>`.

**Мемоизация между вызовами.** `ToolContext` создаётся на сессию, но экземпляр
инструмента переживает много вызовов; тем не менее надёжнее кэш на процесс:

```rust
/// Кэш загруженных баз: путь индекса → готовая база (разбор один раз на процесс).
static DB_CACHE: OnceLock<Mutex<HashMap<PathBuf, Arc<ConceptDb>>>> = OnceLock::new();
```

`ConceptDb::open_cached(path) -> Result<Arc<ConceptDb>>` проверяет кэш, иначе
грузит и кладёт `Arc`. **Ошибки не кэшируются** (повторный вызов повторит
попытку и вернёт ту же диагностику); `HarnessError` не `Clone`, поэтому
сбойный путь в кэш не попадает. Ключ — канонизированный `PathBuf`, так что
тесты с временным фикстуром и прод с `~/library` не мешают друг другу.

**Блокирующий разбор — только в `spawn_blocking`.** `call` инструмента
`async`, а `from_slice` на 127 МБ — блокирующая работа; по образцу
`kb::search_with`:

```rust
let db = tokio::task::spawn_blocking(move || ConceptDb::open_cached(&path))
    .await
    .map_err(|e| HarnessError::Kb(format!("загрузка индекса концептов прервана: {e}")))??;
```

`Arc<ConceptDb>` уходит из `spawn_blocking` свободно (`ConceptDb: Send + Sync`:
одни только `HashMap`/`Box<str>`/`OnceLock`).

**Порядок и объёмы (ожидаемо):** холодный комбинированный разбор
concepts+aliases ~72 МБ — сотни мс…единицы секунд; сканирование хвостовых
54.5 МБ `graph` (без аллокаций) — десятки мс. RSS после: ~150–250 МБ; со
загруженной смежностью — плюс ~60 МБ. Если RSS/старт станут проблемой —
оптимизация (в): интернировать slug'и в одну таблицу `Box<str>` + `u32`-индексы
в рёбрах.

---

## 4. Интерфейс инструмента

### 4.1. Имя и назначение

- **`concept_search`** — «типизированный запрос к базе концептов Ариадны»
  (не `grep`, не `kb_search`): резолв синонимов → карточка → смежность → фасеты.
  Отличие от `kb_search` (BM25 по `[knowledge].dirs`, поиск текста): здесь
  ищется **концепт** с каноническим slug и источником.

### 4.2. Аргументы (один инструмент, диспетчер по `action`)

Единый `ToolSpec` c `action` (стиль `model_query`/fs): одна запись в
`docs/tools.md`, один инструмент в контексте модели.

```jsonc
{
  "type": "object",
  "properties": {
    "action": {"type": "string",
      "enum": ["lookup","resolve","neighbors","search","facets","stats"],
      "description": "lookup — карточка по slug; resolve — термин→канонический slug; neighbors — связи; search — подстрока+фасеты; facets — распределения; stats — сводка индекса"},
    "slug":  {"type": "string", "description": "Канонический slug (lookup/neighbors)"},
    "term":  {"type": "string", "description": "Термин или алиас (resolve); для lookup допустим вместо slug"},
    "query": {"type": "string", "description": "Подстрока для search (регистронезависимо; по slug/title/family + резолв алиаса)"},
    "direction": {"type": "string", "enum": ["in","out","both"], "default": "both",
      "description": "neighbors: входящие / исходящие / оба"},
    "type":     {"type": "string", "description": "Фасет: architectural_component | algorithmic_primitive | data_structure | boundary_condition | procedural_step | diagnostic_metric"},
    "level":    {"type": "string", "description": "Фасет уровня: α | β | γ"},
    "formality":{"type": "string", "description": "Фасет формальности: A | B | C"},
    "family":   {"type": "string", "description": "Фасет семейства (регистронезависимо)"},
    "limit":    {"type": "integer", "default": 10, "description": "Максимум записей (потолок 20)"},
    "sort":     {"type": "string", "enum": ["relevance","centrality","alpha"], "default": "relevance"},
    "body":     {"type": "boolean", "default": false, "description": "lookup: вернуть тело карточки (md) вместо краткой карточки"},
    "format":   {"type": "string", "enum": ["text","json"], "default": "text"}
  },
  "required": ["action"]
}
```

Обязательность полей проверяется вручную (не `required` в схеме для всех
веток): `lookup` требует `slug` или `term`; `neighbors` — `slug`; `search` —
`query`; `resolve` — `term`. Отсутствие — `ToolOutput::err` с подсказкой.

### 4.3. Возврат

**text (по умолчанию)** — модель-читаемый, по образцу `kb_search`:

- `lookup`: `── <slug> [<type> / <level> / <formality> / <family>]`
  + `title`, `file`, `in:<N> out:<M>` (если граф уже загружен — иначе не
  считать), опц. тело.
- `resolve`: `термин → <slug> (точное совпадение slug|алиас|регистронезависимо)`
  либо `не найдено — проверьте термин (нет факта, ML-05)`.
- `neighbors`: `── <slug> (in-degree <K>)` по строке на соседа, помечены `←`/`→`.
- `search`: `── <slug> — <title> [<family>] (in:<K>)`, `limit` строк.
- `facets`: `type: … N` / `level: α N …` / `formality: …` / `family top-20`.

**json** (`format: "json"`) — `ToolOutput::ok(serde_json::to_string_pretty(..))`
с теми же данными в типизированной форме (для слэш-команд/скриптов).
Оба режима усекаются `ToolOutput::truncated(MAX_OUT_CHARS)` (≈8 КБ, как в
других инструментах).

### 4.4. Сигнатуры `src/concept.rs`

```rust
// ─── Константы ───────────────────────────────────────────────────────────
pub const INDEX_FILE: &str = "concept_index.json";
pub const GRAPH_FILE: &str = "concept_graph.json";
pub const MAX_HITS: usize = 20;                 // жёсткий потолок выдачи
pub const DEFAULT_HITS: usize = 10;
pub const MAX_CARD_BYTES: u64 = 1 << 20;        // 1 МБ на тело карточки (не 5, тела мелкие)
pub const MAX_OUT_CHARS: usize = 8 * 1024;      // усечение ToolOutput

// ─── Модель данных (после нормализации) ─────────────────────────────────
/// Карточка концепта (поля индекса; `sources`/`related` — только в md, P1).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Card {
    /// Канонический slug (ключ `concepts`).
    pub slug: String,
    /// Тип концепта (поле индекса `type`).
    #[serde(rename = "type")]
    pub kind: String,
    /// Заголовок (может быть null).
    pub title: Option<String>,
    /// Уровень α/β/γ (может быть null/мусор — нормализуем в trim).
    pub level: Option<String>,
    /// Формальность A/B/C.
    pub formality: Option<String>,
    /// Семейство (пустое → None).
    pub family: Option<String>,
    /// Путь карточки относительно корня библиотеки.
    pub file: String,
}

/// Разновидность совпадения при резолве термина.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MatchKind { Slug, AliasExact, AliasFolded, Fuzzy, None }

/// Результат резолва термина в канонический slug.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolved { pub term: String, pub slug: Option<String>, pub kind: MatchKind }

/// Направление обхода смежности.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction { In, Out, Both }
impl Direction {
    /// `"in"|"out"|"both"` (регистронезависимо).
    pub fn parse(s: &str) -> Result<Self>;
    /// Обратное преобразование (для CLI/JSON).
    pub fn as_str(self) -> &'static str;
}

/// Порядок выдачи.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sort { Relevance, Centrality, Alpha }

/// Фасет-фильтр (все поля — необязательные условия И).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Facets {
    pub kind: Option<String>,
    pub level: Option<String>,
    pub formality: Option<String>,
    pub family: Option<String>,
}

/// Сосед по смежности + его центральность (in-degree).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Neighbor { pub slug: String, pub title: Option<String>,
                      pub kind: Option<String>, pub degree: usize }

/// Сводка индекса.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Stats {
    pub concepts: usize, pub aliases: usize, pub graph_nodes: usize,
    pub by_type: std::collections::BTreeMap<String, usize>,
    pub rebuilt_at: Option<String>,
    /// `stats.total_concepts` индекса — заявлено vs фактически (`concepts`).
    pub reported_total: Option<usize>,
}

/// Счётчики фасетов (для `facets`).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct FacetCounts {
    pub kind: std::collections::BTreeMap<String, usize>,
    pub level: std::collections::BTreeMap<String, usize>,
    pub formality: std::collections::BTreeMap<String, usize>,
    pub family: std::collections::BTreeMap<String, usize>, // обрезается до TOP_N
}

// ─── База ───────────────────────────────────────────────────────────────
/// Загруженный индекс концептов (карточки+алиасы; смежность — лениво).
pub struct ConceptDb { /* приватные поля: cards, aliases, folded, facets, adj */ }

impl ConceptDb {
    /// Разбирает `concept_index.json`; `graph` пропускается (IgnoredAny).
    /// # Errors: файл недоступен/невалиден.
    pub fn load(index_path: &Path) -> Result<Self>;

    /// Мемоизированный `load` (один разбор на процесс, кэш по пути).
    /// # Errors: как у `load`.
    pub fn open_cached(index_path: &Path) -> Result<Arc<Self>>;

    pub fn len(&self) -> usize;
    pub fn is_empty(&self) -> bool;
    pub fn stats(&self) -> Stats;

    /// Карточка по точному slug.
    pub fn lookup(&self, slug: &str) -> Option<&Card>;

    /// Термин → канонический slug: slug → алиас (точно) → алиас
    /// (регистронезависимо/trim). Не найдено — `Resolved{slug:None, kind:None}`
    /// (не ошибка: честное «нет факта», ML-05).
    pub fn resolve(&self, term: &str) -> Resolved;

    /// Поиск: подстрока (регистронезависимо) по slug/title/family + резолв
    /// запроса-алиаса; фасеты — конъюнкция; `limit` усечён до [`MAX_HITS`].
    pub fn search(&self, query: &str, f: &Facets, limit: usize, sort: Sort) -> Vec<&Card>;

    /// Распределение по фасетам (family — топ-N по частоте).
    pub fn facet_counts(&self) -> FacetCounts;

    /// Соседи по смежности. Требует граф (ленивая загрузка рядом с индексом).
    /// # Errors: граф недоступен/невалиден.
    pub fn neighbors(&self, slug: &str, dir: Direction, limit: usize)
        -> Result<Vec<Neighbor>>;

    /// Тело карточки (md), лимит [`MAX_CARD_BYTES`]; путь проверяется
    /// на принадлежность корню библиотеки.
    /// # Errors: выход за корень / файл недоступен.
    pub fn card_body(&self, card: &Card) -> Result<String>;

    /// Frontmatter карточки (`sources`, `related`, `aliases`) — P1.
    /// # Errors: файл недоступен.
    pub fn frontmatter(&self, card: &Card) -> Result<BTreeMap<String, String>>;
}

/// Рендер ответа инструмента (общий с CLI — один источник правды).
pub fn render_lookup(db: &ConceptDb, card: &Card, body: bool) -> String;
pub fn render_resolve(r: &Resolved) -> String;
pub fn render_neighbors(db: &ConceptDb, slug: &str, dir: Direction, ns: &[Neighbor]) -> String;
pub fn render_search(db: &ConceptDb, q: &str, cards: &[&Card]) -> String;
pub fn render_facets(c: &FacetCounts) -> String;
pub fn render_stats(s: &Stats) -> String;

/// Инструменты домена. Пустой вектор, если `[concept].enabled=false` или
/// `index` не задан (гейт на уровне регистрации, как у web/archify).
pub fn tools(cfg: &Config) -> Vec<Arc<dyn Tool>>;
```

Внутренние (приватные) помощники: `struct RawIndexFile/RawConceptIndex/RawCard`
(`Deserialize`), `struct RawGraph/RawNode`, `fn normalize(card) -> Card`
(trim, `""`→`None`), `fn fold_key(s) -> String` (lowercase + trim),
`fn read_frontmatter(text) -> BTreeMap`, `fn under_root(root, rel) -> bool`.

---

## 5. Конфигурация

Новая секция (по образцу `KnowledgeConfig`/`ArchifyConfig`):

```toml
# ── База концептов Ариадны (домен AI/ML) ───────────────────────────────────
# Индекс концептов ~186K (index/concept_index.json, 126 МБ) и граф смежности
# (index/concept_graph.json, 50 МБ). Инструмент concept_search доступен
# агенту, только если enabled=true и задан index. Парсится лениво, один
# раз на процесс. Пути личной машины — только здесь, не в коде (AGENTS).
[concept]
enabled = false                          # true + index → инструмент регистрируется
index    = "~/library/index/concept_index.json"
graph    = "~/library/index/concept_graph.json"   # пусто → sibling от index
max_hits = 20                            # потолок выдачи (≤ MAX_HITS)
```

```rust
/// База концептов Ариадны для `concept_search`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ConceptConfig {
    /// Выключатель: false — инструмент не регистрируется (гейт регистрации).
    pub enabled: bool,
    /// Путь к `concept_index.json` (пусто — инструмент не настраивается).
    pub index: PathBuf,
    /// Путь к `concept_graph.json` (пусто — sibling `index`).
    pub graph: PathBuf,
    /// Потолок числа записей в выдаче.
    pub max_hits: usize,
}
impl Default for ConceptConfig {
    fn default() -> Self {
        Self { enabled: false, index: PathBuf::new(), graph: PathBuf::new(),
               max_hits: MAX_HITS }
    }
}
```

- `expand_tildes()` (config.rs, ~1121) — добавить `expand(&mut self.concept.index)`
  и `expand(&mut self.concept.graph)`.
- `Config::graph_path()` — резолв: `graph`, иначе `index.with_file_name(GRAPH_FILE)`.
- **Решение по дефолту:** `enabled = false` (в отличие от `[web]`/`[archify]`).
  Обоснование: индекс — внешний, машинно-специфичный артефакт, в репозитории
  личных путей быть не должно (AGENTS); пресет ML-исследователя
  (`aiml/presets/`) включает его данными. Альтернатива (как у archify:
  enabled=true + пустой путь → инструмент отвечает инструкцией «не настроено»)
  отклонена: инструмент-заглушка в контексте обычного архитектора — шум.

---

## 6. Точка регистрации

1. `src/lib.rs` — `pub mod concept;` (алфавитный порядок между `contract_diff`
   и `control`).
2. `src/tools.rs::domain_tools(cfg)` — после `out.extend(crate::kb::tools());`:

```rust
// База концептов Ариадны (домен AI/ML): при `[concept].enabled=false`
// или незаданном index инструмент не регистрируется — гейт на уровне
// регистрации (как web/archify), сам инструмент о конфиге не знает (AD-4).
out.extend(crate::concept::tools(cfg));
```

3. Тест в `src/tools.rs` — расширить
   `full_registry_contains_core_interaction_and_domain_tools` строкой
   `"concept_search"`? **Нет**: при дефолтном конфиге (enabled=false)
   инструмента нет. Вместо этого — парный тест
   `full_registry_omits_concept_tools_when_disabled` +
   `full_registry_keeps_concept_tool_when_configured` (по образцу web/archify).

---

## 7. CLI-подкоманда `arch-ml concept`

```text
arch-ml concept lookup <slug> [--body] [--json] [--index <path>]
arch-ml concept resolve <term> [--json] [--index <path>]
arch-ml concept neighbors <slug> [--direction in|out|both] [--limit N] [--json]
arch-ml concept search [--query Q] [--type T] [--level α] [--formality C]
                       [--family F] [--limit N] [--sort relevance|centrality|alpha] [--json]
arch-ml concept stats [--json]
```

- `src/main.rs`: `enum Cmd { …, /// База концептов Ариадны. Concept { #[command(subcommand)] cmd: ConceptCmd }, … }`
  + `#[derive(Subcommand)] enum ConceptCmd { Lookup{…}, Resolve{…}, Neighbors{…}, Search{…}, Stats{…} }`
  + `fn cmd_concept(cmd: ConceptCmd) -> anyhow::Result<()>` в диспетчере
  (`Some(Cmd::Concept{cmd}) => cmd_concept(cmd)?`).
- CLI и инструмент используют **одни и те же** `ConceptDb::*` и `render_*`.
- `--index` переопределяет `[concept].index` (в тестах — фикстурный файл).
- Ненастроенный индекс: понятная ошибка + exit 1 (не паника); пустой результат
  поиска — exit 0 с «не найдено».
- Вспомогательное: `fn concept_index_path(cfg: &Config, cli: Option<PathBuf>) -> Result<PathBuf>`.

---

## 8. Открытые вопросы (решить при реализации, не блокируют P0)

1. **Природа рёбер.** `edges_in/out` — голые slug'и без типа связи; направление
   семантически не определено индексом. P0: отдавать как есть, с in-degree как
   сигналом центральности (GRPO = 514 in-edges). P1: уточнить по генератору
   библиотеки, есть ли типы рёбер.
2. **`stats.total_concepts` ≠ число карточек** (186 686 vs 184 386). P0:
   показывать оба в `stats` с пометкой; не использовать `stats` для лимитов.
3. **`maturity` всегда null** — не выводить и не фильтровать по нему до
   появления данных.
4. **`sources`/`related`** — только в md (P1: frontmatter-парсер; P0: доступны
   через `body=true`). Ссылка на первоисточник обязательна для контракта
   «гипотеза со ссылкой» (ML-05/ML-11) — до P1 помечать текстовый вывод
   `источник: см. body=true`.
5. **Стоимость холодного старта** (~сотни мс…секунды на 72 МБ) — если станет
   проблемой на каждый запуск CLI, перейти к варианту (в): компактный кэш в
   `paths.state_dir` со штампом `{rebuilt_at, mtime, len}`.

---

## 9. Список тестов

### 9.1. Unit — `src/concept.rs` (`#[cfg(test)]`, фикстурный индекс в `tempfile`)

Фикстура: мини-индекс на 6 карточек (включая `title:null`, `family:""`,
`level:null`, алиасы `π0.5`→`0_5_model`, `GRPO`→`group_relative_policy_optimization`,
алиас с другим регистром), блок `graph.nodes` с непустыми `edges_in/out` и
`edges: []`, `stats` с завышенным `total_concepts`; опционально md-файлы.

| # | Тест | Проверяет |
|---|---|---|
| 1 | `load_reads_cards_aliases` | Число карточек/алиасов, поля, null-безопасность, `""`→`None` |
| 2 | `load_does_not_require_graph_file` | Разбор удаётся без `concept_graph.json`; `neighbors` тогда — мягкая ошибка |
| 3 | `load_ignores_graph_block` | Фикстура с `graph` (валидный шейп) грузится; `graph_nodes` до `neighbors` = 0/неизвестно, `stats` не падает |
| 4 | `load_missing_file_is_soft_error` | `HarnessError::Kb`, не паника |
| 5 | `load_malformed_json_is_soft_error` | Битый JSON → ошибка, не паника |
| 6 | `resolve_exact_slug_and_alias` | `Slug`, `AliasExact`; `π0.5`→`0_5_model` |
| 7 | `resolve_folded_case_insensitive` | `grpo`→slug, `AliasFolded`; пробелы/регистр |
| 8 | `resolve_unknown_is_none_not_error` | `MatchKind::None`, `slug: None` |
| 9 | `lookup_returns_card_fields_and_file` | Поля карточки, `file` относительный |
| 10 | `search_substring_case_insensitive` | Совпадение по slug/title/family, регистр |
| 11 | `search_facets_conjunction` | Комбинации `type`/`level`/`formality`/`family`; пустой результат — ок |
| 12 | `search_family_case_insensitive` | «evaluation metrics» ≈ «Evaluation Metrics» |
| 13 | `search_limit_capped_at_max_hits` | `limit=999` → ≤ `MAX_HITS` |
| 14 | `sort_centrality_and_alpha` | Порядок выдачи |
| 15 | `neighbors_direction_in_out_both` | Три направления, `←`/`→`, дедупликация в `both` |
| 16 | `neighbors_limit_and_degree` | `limit`, `degree` = len(`edges_in`) соседа |
| 17 | `neighbors_unknown_slug_is_empty` | Пустой вектор, не ошибка |
| 18 | `facets_counts_match_fixture` | Счётчики по всем четырём фасетам |
| 19 | `card_body_reads_md_and_caps` | Тело читается; лимит `MAX_CARD_BYTES` |
| 20 | `card_body_rejects_path_escape` | `file: "../etc/passwd"` → ошибка |
| 21 | `open_cached_returns_same_arc` | `Arc::ptr_eq` — мемоизация по пути |
| 22 | `tools_gated_by_config` | disabled/пустой index → `[]`; enabled+index → `["concept_search"]` |
| 23 | `tool_spec_has_action_enum_and_required` | Схема: `action`, enum действий |
| 24 | `tool_call_lookup_text_and_json` | Через `dispatch`: text и `format=json` — валидный JSON |
| 25 | `tool_call_invalid_action_and_missing_args` | `ToolOutput::err` с подсказкой, не паника |
| 26 | `frontmatter_parses_sources_and_related` | P1: `sources:[arxiv.…]`, дубль-блок → последний |
| 27 | `frontmatter_missing_is_empty` | Нет фронтматтера → пустая карта |

### 9.2. Unit — `src/config.rs`

| # | Тест | Проверяет |
|---|---|---|
| 28 | `default_concept_config_disabled` | `enabled=false`, пустые пути, `max_hits=MAX_HITS` |
| 29 | `concept_paths_expand_tilde` | `~/library/...` → домашний каталог после `expand_tildes` |

### 9.3. Unit — `src/tools.rs`

| # | Тест | Проверяет |
|---|---|---|
| 30 | `full_registry_omits_concept_tools_when_disabled` | Дефолт: `concept_search` нет |
| 31 | `full_registry_keeps_concept_tool_when_configured` | `enabled=true`+index → есть |

### 9.4. Интеграционные — `tests/cli.rs`

| # | Тест | Проверяет |
|---|---|---|
| 32 | `concept_lookup_prints_card` | `arch-ml concept lookup <slug> --index <fixture>` → поля карточки, exit 0 |
| 33 | `concept_search_json_is_valid` | `--json` → парсится как JSON, массив хитов |
| 34 | `concept_unconfigured_index_exits_1` | Без `[concept]` — понятное сообщение, exit 1, без паники |

Все тесты офлайн и детерминированы (фикстуры в `tempfile`, живые LLM/сеть не
зовутся) — как требует `tests/cli.rs`.

---

## 10. Какие файлы править (чек-лист реализации)

| Файл | Действие | Объём |
|---|---|---|
| `src/concept.rs` | **Новый модуль**: схема, `ConceptDb`, резолв, поиск, фасеты, смежность, рендер, инструмент, тесты | ~700–900 строк |
| `src/lib.rs` | `pub mod concept;` | 1 строка |
| `src/tools.rs` | `out.extend(crate::concept::tools(cfg));` + 2 теста гейта | ~15 строк |
| `src/config.rs` | `ConceptConfig` + поле в `Config` + `Default` + `expand_tildes` + 2 теста | ~60 строк |
| `src/main.rs` | `Cmd::Concept` + `enum ConceptCmd` + `fn cmd_concept` + диспетчер | ~120 строк |
| `config.example.toml` | Секция `[concept]` с комментариями | ~10 строк |
| `docs/tools.md` | Строка `concept_search` в доменном разделе (Назначение + Параметры) | 1 строка |
| `tests/cli.rs` | 3 интеграционных теста | ~60 строк |
| `aiml/presets/ml-researcher/*` | Пресет включает `[concept]` (P1, опционально) | — |

**Порядок работ:** 1) схема+`ConceptDb::load`+тесты 1–5, 28–29 → 2) резолв/поиск/
фасеты + 6–18 → 3) смежность + 15–17 → 4) инструмент + 22–25, 30–31 →
5) CLI + 32–34 → 6) docs/config → 7) `cargo check` + `cargo test` (зелёные,
clippy `-D warnings`).

**Инварианты, которые нельзя нарушить:** без `unsafe`; без `unwrap`/`expect`
вне тестов; сбой → `ToolOutput::err`/`HarnessError`, не паника; ядро не
зависит от `aiml/` (AD-1); личные пути — только в пользовательском конфиге;
новых зависимостей нет.
