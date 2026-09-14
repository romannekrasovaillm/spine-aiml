//! База концептов Ариадны: типизированный поиск по индексу ML-концептов.
//!
//! Индекс — внешний, машинно-специфичный артефакт (`concept_index.json`,
//! ~126 МБ) и граф смежности (`concept_graph.json`, ~50 МБ). Оба пути живут
//! только в пользовательском конфиге (`[concept]`), в коде репозитория личных
//! путей нет (AGENTS). Инструмент [`tools`] не регистрируется, пока не задан
//! `[concept].index` при `enabled = true` — гейт на уровне регистрации, как у
//! web/archify.
//!
//! Ловушки схемы (проверено на живом индексе, см.
//! `aiml/notes/concept-search-spec.md` §1):
//! - `graph.edges` **всегда** пустой массив: топология только в
//!   `nodes.*.edges_in` / `nodes.*.edges_out`;
//! - `stats.total_concepts` (186 686) не равен числу карточек (184 386) —
//!   источник истины `len(concepts)`, `stats` отдаём как «заявлено индексом»;
//! - фасеты грязные: `level`/`formality`/`family` бывают `null`/`""`/разного
//!   регистра — нормализуем при загрузке, фильтруем с учётом регистра;
//! - `maturity` всегда `null` — не закладываемся;
//! - `file` — путь от корня библиотеки; любые чтения только после проверки,
//!   что канонизированный путь лежит под корнем (защита от `..`).
//!
//! Карточки и алиасы разбираются при открытии БД (разовый проход по индексу,
//! ~126 МБ); смежность — лениво, при первом [`ConceptDb::neighbors`]. Результат
//! мемоизируется на процесс ([`ConceptDb::open_cached`]).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

// ─── Константы ───────────────────────────────────────────────────────────

/// Имя файла индекса концептов по умолчанию (в каталоге `index/` библиотеки).
pub const INDEX_FILE: &str = "concept_index.json";
/// Имя файла графа смежности (сосед индекса, если путь не задан в конфиге).
pub const GRAPH_FILE: &str = "concept_graph.json";
/// Жёсткий потолок числа записей в выдаче.
pub const MAX_HITS: usize = 20;
/// Число записей по умолчанию.
pub const DEFAULT_HITS: usize = 10;
/// Лимит тела карточки, байты (тела мелкие — 1 МБ с запасом).
pub const MAX_CARD_BYTES: u64 = 1 << 20;
/// Усечение текстового вывода инструмента, символы.
pub const MAX_OUT_CHARS: usize = 8 * 1024;
/// Сколько семейств показывать в `facets` (топ по частоте).
const TOP_FAMILY: usize = 20;
/// Метка отсутствующего значения фасета в счётчиках.
const NO_VALUE: &str = "(нет)";
/// Заголовок отсутствует (для рендера).
const NO_TITLE: &str = "(без заголовка)";
/// Тип концепта отсутствует.
const NO_KIND: &str = "(без типа)";

// ─── Модель данных (после нормализации) ──────────────────────────────────

/// Карточка концепта (поля индекса; `sources`/`related` — только в md).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Card {
    /// Канонический slug (ключ `concepts`).
    pub slug: String,
    /// Тип концепта (поле индекса `type`).
    #[serde(rename = "type")]
    pub kind: String,
    /// Заголовок (может отсутствовать).
    pub title: Option<String>,
    /// Уровень (`α`/`β`/`γ`; бывает `null`/мусор — приведён к `trim`).
    pub level: Option<String>,
    /// Формальность (`A`/`B`/`C`).
    pub formality: Option<String>,
    /// Семейство (пустое → `None`).
    pub family: Option<String>,
    /// Путь карточки относительно корня библиотеки.
    pub file: String,
}

/// Разновидность совпадения при резолве термина.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MatchKind {
    /// Точное совпадение slug.
    Slug,
    /// Точное совпадение алиаса (регистрозначимо).
    AliasExact,
    /// Совпадение алиаса/слага без учёта регистра и краевых пробелов.
    AliasFolded,
    /// Единственное подстрочное совпадение (нечёткое).
    Fuzzy,
    /// Не найдено (честное «нет факта», не ошибка).
    None,
}

/// Результат резолва термина в канонический slug.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolved {
    /// Исходный термин (как передал вызывающий).
    pub term: String,
    /// Канонический slug, если найден.
    pub slug: Option<String>,
    /// Разновидность совпадения.
    pub kind: MatchKind,
}

/// Направление обхода смежности.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    /// Входящие рёбра (`edges_in`).
    In,
    /// Исходящие рёбра (`edges_out`).
    Out,
    /// Оба направления (дедупликация).
    Both,
}

impl Direction {
    /// Разбирает `"in"|"out"|"both"` (регистронезависимо).
    ///
    /// # Errors
    /// Незнакомое значение — [`HarnessError::Tool`] с подсказкой.
    pub fn parse(s: &str) -> Result<Self> {
        match s.trim().to_lowercase().as_str() {
            "in" => Ok(Self::In),
            "out" => Ok(Self::Out),
            "both" | "" => Ok(Self::Both),
            other => Err(HarnessError::Tool(format!(
                "direction: ожидалось in|out|both, получено «{other}»"
            ))),
        }
    }

    /// Обратное преобразование (для CLI/JSON).
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::In => "in",
            Self::Out => "out",
            Self::Both => "both",
        }
    }
}

/// Порядок выдачи результатов поиска.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sort {
    /// По релевантности (оценка совпадения, затем slug).
    Relevance,
    /// По центральности (in-degree, затем slug).
    Centrality,
    /// По алфавиту slug.
    Alpha,
}

impl Sort {
    /// Разбирает `"relevance"|"centrality"|"alpha"` (регистронезависимо).
    ///
    /// # Errors
    /// Незнакомое значение — [`HarnessError::Tool`] с подсказкой.
    pub fn parse(s: &str) -> Result<Self> {
        match s.trim().to_lowercase().as_str() {
            "relevance" | "" => Ok(Self::Relevance),
            "centrality" => Ok(Self::Centrality),
            "alpha" => Ok(Self::Alpha),
            other => Err(HarnessError::Tool(format!(
                "sort: ожидалось relevance|centrality|alpha, получено «{other}»"
            ))),
        }
    }

    /// Обратное преобразование (для CLI/JSON).
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Relevance => "relevance",
            Self::Centrality => "centrality",
            Self::Alpha => "alpha",
        }
    }
}

/// Фасет-фильтр (все поля — необязательные условия И).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Facets {
    /// Тип концепта.
    pub kind: Option<String>,
    /// Уровень (`α`/`β`/`γ`).
    pub level: Option<String>,
    /// Формальность (`A`/`B`/`C`).
    pub formality: Option<String>,
    /// Семейство (сравнение регистронезависимо).
    pub family: Option<String>,
}

impl Facets {
    /// Есть ли хотя бы одно условие.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.kind.is_none()
            && self.level.is_none()
            && self.formality.is_none()
            && self.family.is_none()
    }

    /// Проверяет карточку против всех заданных условий (И).
    fn matches(&self, card: &Card) -> bool {
        let eq = |facet: &Option<String>, value: &Option<String>| match facet {
            None => true,
            Some(want) => value
                .as_deref()
                .is_some_and(|v| v == want || fold_key(v) == fold_key(want)),
        };
        let kind = match &self.kind {
            None => true,
            Some(want) => card.kind == *want || fold_key(&card.kind) == fold_key(want),
        };
        kind && eq(&self.level, &card.level)
            && eq(&self.formality, &card.formality)
            && eq(&self.family, &card.family)
    }
}

/// Сосед по смежности + его центральность (in-degree).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Neighbor {
    /// Slug соседа.
    pub slug: String,
    /// Заголовок соседа (из карточек индекса).
    pub title: Option<String>,
    /// Тип соседа.
    pub kind: Option<String>,
    /// In-degree соседа (число входящих рёбер).
    pub degree: usize,
}

/// Сводка индекса.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Stats {
    /// Число карточек (источник истины — `len(concepts)`).
    pub concepts: usize,
    /// Число алиасов.
    pub aliases: usize,
    /// Число узлов графа (0, пока смежность не загружена).
    pub graph_nodes: usize,
    /// Заявленное распределение по типам (`stats.by_type` индекса).
    pub by_type: BTreeMap<String, usize>,
    /// Штамп пересборки индекса.
    pub rebuilt_at: Option<String>,
    /// `stats.total_concepts` индекса — заявлено vs фактически (`concepts`).
    pub reported_total: Option<usize>,
}

/// Счётчики фасетов (для `facets`).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct FacetCounts {
    /// Распределение по типам.
    pub kind: BTreeMap<String, usize>,
    /// Распределение по уровням.
    pub level: BTreeMap<String, usize>,
    /// Распределение по формальности.
    pub formality: BTreeMap<String, usize>,
    /// Распределение по семействам (обрезано до топ-20 по частоте).
    pub family: BTreeMap<String, usize>,
}

// ─── Сырые структуры индекса ─────────────────────────────────────────────

/// Верхний уровень `concept_index.json` (только нужные блоки; `graph`
/// пропускается serde'ом как неизвестное поле).
#[derive(Debug, Default, Deserialize)]
struct RawIndexFile {
    /// Блок карточек и алиасов.
    #[serde(default)]
    concept_index: RawConceptIndex,
    /// Блок сводки.
    #[serde(default)]
    stats: RawStats,
}

/// `concept_index`: карточки по slug + алиасы.
#[derive(Debug, Default, Deserialize)]
struct RawConceptIndex {
    /// Карточки `<slug>` → карточка.
    #[serde(default)]
    concepts: HashMap<String, RawCard>,
    /// Алиасы `<alias>` → `<slug>`.
    #[serde(default)]
    aliases: HashMap<String, String>,
}

/// Сырая карточка (все поля могут отсутствовать/быть `null`).
#[derive(Debug, Default, Deserialize)]
struct RawCard {
    /// Тип концепта.
    #[serde(default, rename = "type")]
    kind: Option<String>,
    /// Путь карточки от корня библиотеки.
    #[serde(default)]
    file: Option<String>,
    /// Заголовок.
    #[serde(default)]
    title: Option<String>,
    /// Уровень.
    #[serde(default)]
    level: Option<String>,
    /// Формальность.
    #[serde(default)]
    formality: Option<String>,
    /// Семейство.
    #[serde(default)]
    family: Option<String>,
}

/// Блок `stats` индекса.
#[derive(Debug, Default, Deserialize)]
struct RawStats {
    /// Заявленное число концептов (≠ числу карточек).
    #[serde(default)]
    total_concepts: Option<usize>,
    /// Заявленное распределение по типам.
    #[serde(default)]
    by_type: BTreeMap<String, usize>,
    /// Штамп пересборки.
    #[serde(default)]
    rebuilt_at: Option<String>,
}

/// `concept_graph.json` (или блок `graph` индекса): только adjacency.
#[derive(Debug, Default, Deserialize)]
struct RawGraph {
    /// Узлы `<slug>` → узел смежности.
    #[serde(default)]
    nodes: HashMap<String, RawNode>,
}

/// Узел смежности: `type`/`title` игнорируются при разборе.
#[derive(Debug, Default, Deserialize)]
struct RawNode {
    /// Входящие рёбра (slug'и).
    #[serde(default)]
    edges_in: Vec<String>,
    /// Исходящие рёбра (slug'и).
    #[serde(default)]
    edges_out: Vec<String>,
}

// ─── Смежность (ленивый слой) ────────────────────────────────────────────

/// Индекс смежности: `slug` → списки соседей.
#[derive(Debug, Default)]
struct AdjIndex {
    /// Узлы графа.
    nodes: HashMap<String, AdjNode>,
}

/// Соседи одного узла.
#[derive(Debug, Default)]
struct AdjNode {
    /// Входящие рёбра.
    edges_in: Vec<String>,
    /// Исходящие рёбра.
    edges_out: Vec<String>,
}

// ─── База ────────────────────────────────────────────────────────────────

/// Кэш загруженных баз: путь индекса → готовая база (разбор один раз на
/// процесс). Ошибки не кэшируются.
static DB_CACHE: OnceLock<Mutex<HashMap<PathBuf, Arc<ConceptDb>>>> = OnceLock::new();

/// Загруженный индекс концептов (карточки + алиасы; смежность — лениво).
pub struct ConceptDb {
    /// Путь разобранного индекса.
    index_path: PathBuf,
    /// Путь графа смежности.
    graph_path: PathBuf,
    /// Корень библиотеки (`index_path.parent().parent()`).
    root: PathBuf,
    /// Карточки по slug.
    cards: BTreeMap<String, Card>,
    /// Точные алиасы: `<alias>` → `<slug>`.
    aliases: HashMap<String, String>,
    /// Свёрнутые алиасы (lowercase + trim) → slug.
    folded: HashMap<String, String>,
    /// Свёрнутые slug'и → канонический slug.
    folded_slugs: HashMap<String, String>,
    /// Заявленное распределение по типам.
    by_type: BTreeMap<String, usize>,
    /// Штамп пересборки индекса.
    rebuilt_at: Option<String>,
    /// Заявленное число концептов.
    reported_total: Option<usize>,
    /// Счётчики фасетов.
    facets: FacetCounts,
    /// Ленивая смежность (ошибка хранится строкой: `HarnessError` не `Clone`).
    adj: OnceLock<std::result::Result<Arc<AdjIndex>, String>>,
}

impl std::fmt::Debug for ConceptDb {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ConceptDb")
            .field("index_path", &self.index_path)
            .field("cards", &self.cards.len())
            .field("aliases", &self.aliases.len())
            .field("adj_loaded", &self.adj.get().is_some())
            .finish()
    }
}

impl ConceptDb {
    /// Разбирает `concept_index.json`; блок `graph` пропускается. Граф
    /// смежности ищется рядом с индексом ([`GRAPH_FILE`]).
    ///
    /// # Errors
    /// Файл недоступен или JSON невалиден — [`HarnessError::Kb`].
    pub fn load(index_path: &Path) -> Result<Self> {
        let graph_path = index_path.with_file_name(GRAPH_FILE);
        Self::load_paths(index_path, &graph_path)
    }

    /// Разбирает индекс с явным путём к графу смежности.
    ///
    /// # Errors
    /// Файл недоступен или JSON невалиден — [`HarnessError::Kb`].
    pub fn load_paths(index_path: &Path, graph_path: &Path) -> Result<Self> {
        let bytes = std::fs::read(index_path).map_err(|e| {
            HarnessError::Kb(format!(
                "индекс концептов не читается {}: {e}",
                index_path.display()
            ))
        })?;
        let raw: RawIndexFile = serde_json::from_slice(&bytes)
            .map_err(|e| HarnessError::Kb(format!("разбор {}: {e}", index_path.display())))?;
        Ok(Self::from_raw(index_path, graph_path, raw))
    }

    /// Собирает базу из разобранного сырого файла (нормализация + фасеты).
    fn from_raw(index_path: &Path, graph_path: &Path, raw: RawIndexFile) -> Self {
        let mut cards: BTreeMap<String, Card> = BTreeMap::new();
        for (slug, rc) in raw.concept_index.concepts {
            let slug = slug.trim().to_owned();
            if slug.is_empty() {
                continue;
            }
            cards.insert(slug.clone(), normalize(slug, rc));
        }
        let mut aliases: HashMap<String, String> = HashMap::new();
        let mut folded: HashMap<String, String> = HashMap::new();
        for (alias, slug) in raw.concept_index.aliases {
            let slug = slug.trim().to_owned();
            if slug.is_empty() {
                continue;
            }
            let alias = alias.trim().to_owned();
            if alias.is_empty() {
                continue;
            }
            folded
                .entry(fold_key(&alias))
                .or_insert_with(|| slug.clone());
            aliases.insert(alias, slug);
        }
        let folded_slugs = cards
            .keys()
            .map(|s| (fold_key(s), s.clone()))
            .collect::<HashMap<_, _>>();
        let facets = count_facets(cards.values());
        let root = index_path
            .parent()
            .and_then(Path::parent)
            .unwrap_or_else(|| Path::new(""))
            .to_path_buf();
        Self {
            index_path: index_path.to_path_buf(),
            graph_path: graph_path.to_path_buf(),
            root,
            cards,
            aliases,
            folded,
            folded_slugs,
            by_type: raw.stats.by_type,
            rebuilt_at: raw.stats.rebuilt_at,
            reported_total: raw.stats.total_concepts,
            facets,
            adj: OnceLock::new(),
        }
    }

    /// Мемоизированный [`ConceptDb::load`] (один разбор на процесс).
    ///
    /// # Errors
    /// Как у [`ConceptDb::load`].
    pub fn open_cached(index_path: &Path) -> Result<Arc<Self>> {
        let graph = index_path.with_file_name(GRAPH_FILE);
        Self::open_cached_paths(index_path, &graph)
    }

    /// Мемоизированный разбор с явным путём к графу.
    ///
    /// # Errors
    /// Как у [`ConceptDb::load`].
    pub fn open_cached_paths(index_path: &Path, graph_path: &Path) -> Result<Arc<Self>> {
        let key = index_path
            .canonicalize()
            .unwrap_or_else(|_| index_path.to_path_buf());
        if let Some(hit) = cache_get(&key) {
            return Ok(hit);
        }
        let db = Arc::new(Self::load_paths(index_path, graph_path)?);
        cache_put(key, &db);
        Ok(db)
    }

    /// Число карточек.
    #[must_use]
    pub fn len(&self) -> usize {
        self.cards.len()
    }

    /// Пустая ли база.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.cards.is_empty()
    }

    /// Путь разобранного индекса.
    #[must_use]
    pub fn index_path(&self) -> &Path {
        &self.index_path
    }

    /// Корень библиотеки (родитель каталога `index/`).
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Счётчики фасетов (для отчёта `facets`).
    #[must_use]
    pub fn facets(&self) -> &FacetCounts {
        &self.facets
    }

    /// Сводка индекса (число узлов графа — 0, пока смежность не загружена).
    #[must_use]
    pub fn stats(&self) -> Stats {
        Stats {
            concepts: self.cards.len(),
            aliases: self.aliases.len(),
            graph_nodes: self.loaded_adj().map_or(0, |a| a.nodes.len()),
            by_type: self.by_type.clone(),
            rebuilt_at: self.rebuilt_at.clone(),
            reported_total: self.reported_total,
        }
    }

    /// Карточка по точному slug.
    #[must_use]
    pub fn lookup(&self, slug: &str) -> Option<&Card> {
        self.cards.get(slug.trim())
    }

    /// Термин → канонический slug: точный slug → алиас (точно) → алиас
    /// (регистронезависимо/trim) → единственное нечёткое совпадение.
    /// Не найдено — `Resolved { slug: None, kind: None }` (не ошибка, ML-05).
    #[must_use]
    pub fn resolve(&self, term: &str) -> Resolved {
        let t = term.trim();
        let found = |slug: String, kind: MatchKind| Resolved {
            term: term.to_owned(),
            slug: Some(slug),
            kind,
        };
        if self.cards.contains_key(t) {
            return found(t.to_owned(), MatchKind::Slug);
        }
        if let Some(slug) = self.aliases.get(t) {
            return found(slug.clone(), MatchKind::AliasExact);
        }
        let key = fold_key(t);
        if key.is_empty() {
            return unknown(term);
        }
        if let Some(slug) = self.folded.get(&key) {
            return found(slug.clone(), MatchKind::AliasFolded);
        }
        if let Some(slug) = self.folded_slugs.get(&key) {
            return found(slug.clone(), MatchKind::Slug);
        }
        // Нечётко: единственное вхождение запроса в slug (или наоборот).
        // Порог 4 символа с обеих сторон: короткий slug («xyz», «sft») не
        // должен ловиться подстрокой в мусорном слове («xyzzy» → unknown),
        // а опечатка в длинном термине («attentio» → «attention») — должна.
        let mut hit: Option<&String> = None;
        let mut ambiguous = false;
        if key.len() >= 4 {
            for (folded_slug, slug) in &self.folded_slugs {
                if folded_slug.len() < 4 {
                    continue;
                }
                if folded_slug.contains(&key) || key.contains(folded_slug.as_str()) {
                    if hit.is_some() {
                        ambiguous = true;
                        break;
                    }
                    hit = Some(slug);
                }
            }
        }
        if !ambiguous {
            if let Some(slug) = hit {
                return found(slug.clone(), MatchKind::Fuzzy);
            }
        }
        unknown(term)
    }

    /// Лениво грузит смежность из графа (мемоизируется на процесс).
    fn loaded_adj(&self) -> Option<&AdjIndex> {
        self.adj
            .get_or_init(|| {
                load_adj(&self.graph_path)
                    .map(Arc::new)
                    .map_err(|e| e.to_string())
            })
            .as_ref()
            .ok()
            .map(std::convert::AsRef::as_ref)
    }

    /// Соседи узла по смежности, отсортированы по in-degree (центральности).
    #[must_use]
    pub fn neighbors(&self, slug: &str, dir: Direction) -> Vec<Neighbor> {
        let Some(adj) = self.loaded_adj() else {
            return Vec::new();
        };
        let Some(node) = adj.nodes.get(slug.trim()) else {
            return Vec::new();
        };
        let mut seen: HashSet<String> = HashSet::new();
        let mut slugs: Vec<String> = Vec::new();
        let mut push = |list: &[String]| {
            for s in list {
                if seen.insert(s.clone()) {
                    slugs.push(s.clone());
                }
            }
        };
        match dir {
            Direction::In => push(&node.edges_in),
            Direction::Out => push(&node.edges_out),
            Direction::Both => {
                push(&node.edges_in);
                push(&node.edges_out);
            }
        }
        let mut out: Vec<Neighbor> = slugs
            .into_iter()
            .map(|s| {
                let degree = adj.nodes.get(&s).map_or(0, |n| n.edges_in.len());
                let card = self.cards.get(&s);
                Neighbor {
                    slug: s,
                    title: card.and_then(|c| c.title.clone()),
                    kind: card.map(|c| c.kind.clone()).filter(|k| !k.is_empty()),
                    degree,
                }
            })
            .collect();
        out.sort_by(|a, b| b.degree.cmp(&a.degree).then_with(|| a.slug.cmp(&b.slug)));
        out
    }

    /// Поиск по слагу/заголовку с фасетами, сортировкой и лимитом.
    #[must_use]
    pub fn search(&self, query: &str, facets: &Facets, sort: Sort, limit: usize) -> Vec<&Card> {
        let q = query.trim().to_lowercase();
        let mut hits: Vec<&Card> = self
            .cards
            .values()
            .filter(|c| facets.matches(c))
            .filter(|c| {
                q.is_empty()
                    || c.slug.to_lowercase().contains(&q)
                    || c.title
                        .as_deref()
                        .is_some_and(|t| t.to_lowercase().contains(&q))
            })
            .collect();
        let degree = |slug: &str| -> usize {
            self.loaded_adj()
                .and_then(|a| a.nodes.get(slug))
                .map_or(0, |n| n.edges_in.len())
        };
        match sort {
            Sort::Alpha => hits.sort_by(|a, b| a.slug.cmp(&b.slug)),
            Sort::Centrality => hits.sort_by(|a, b| {
                degree(&b.slug)
                    .cmp(&degree(&a.slug))
                    .then_with(|| a.slug.cmp(&b.slug))
            }),
            Sort::Relevance => hits.sort_by(|a, b| {
                let ra = i32::from(!a.slug.to_lowercase().contains(&q));
                let rb = i32::from(!b.slug.to_lowercase().contains(&q));
                ra.cmp(&rb).then_with(|| a.slug.cmp(&b.slug))
            }),
        }
        hits.truncate(limit.min(MAX_HITS));
        hits
    }
}

/// Результат «не найдено».
fn unknown(term: &str) -> Resolved {
    Resolved {
        term: term.to_owned(),
        slug: None,
        kind: MatchKind::None,
    }
}

/// Нормализует сырую карточку: `trim`, `""`/`null` → `None`.
fn normalize(slug: String, raw: RawCard) -> Card {
    Card {
        slug,
        kind: nonempty(raw.kind).unwrap_or_default(),
        title: nonempty(raw.title),
        level: nonempty(raw.level),
        formality: nonempty(raw.formality),
        family: nonempty(raw.family),
        file: raw.file.unwrap_or_default().trim().to_owned(),
    }
}

/// `trim`, `""`/`None` → `None`.
fn nonempty(value: Option<String>) -> Option<String> {
    value.map(|v| v.trim().to_owned()).filter(|v| !v.is_empty())
}

/// Свёрнутый ключ сравнения: lowercase + trim.
fn fold_key(value: &str) -> String {
    value.trim().to_lowercase()
}

/// Счётчики фасетов по карточкам (family обрезается до топ-20).
fn count_facets<'a>(cards: impl Iterator<Item = &'a Card>) -> FacetCounts {
    let mut kind = BTreeMap::new();
    let mut level = BTreeMap::new();
    let mut formality = BTreeMap::new();
    let mut family: BTreeMap<String, usize> = BTreeMap::new();
    for card in cards {
        *kind
            .entry(or_no_value(Some(card.kind.as_str())))
            .or_insert(0) += 1;
        *level.entry(or_no_value(card.level.as_deref())).or_insert(0) += 1;
        *formality
            .entry(or_no_value(card.formality.as_deref()))
            .or_insert(0) += 1;
        *family
            .entry(or_no_value(card.family.as_deref()))
            .or_insert(0) += 1;
    }
    if family.len() > TOP_FAMILY {
        let mut ranked: Vec<(String, usize)> = family.into_iter().collect();
        ranked.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        ranked.truncate(TOP_FAMILY);
        family = ranked.into_iter().collect();
    }
    FacetCounts {
        kind,
        level,
        formality,
        family,
    }
}

/// Метка фасета: пустое/отсутствующее → [`NO_VALUE`].
fn or_no_value(value: Option<&str>) -> String {
    match value {
        Some(v) if !v.trim().is_empty() => v.trim().to_owned(),
        _ => NO_VALUE.to_owned(),
    }
}

/// Чтение из процессного кэша баз.
fn cache_get(key: &Path) -> Option<Arc<ConceptDb>> {
    let cache = DB_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let guard = cache.lock().ok()?;
    guard.get(key).cloned()
}

/// Запись в процессный кэш баз.
fn cache_put(key: PathBuf, db: &Arc<ConceptDb>) {
    let cache = DB_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    if let Ok(mut guard) = cache.lock() {
        guard.insert(key, Arc::clone(db));
    }
}

/// Разбирает граф смежности `concept_graph.json`.
fn load_adj(graph_path: &Path) -> Result<AdjIndex> {
    let bytes = std::fs::read(graph_path).map_err(|e| {
        HarnessError::Kb(format!(
            "граф концептов не читается {}: {e}",
            graph_path.display()
        ))
    })?;
    let raw: RawGraph = serde_json::from_slice(&bytes)
        .map_err(|e| HarnessError::Kb(format!("разбор {}: {e}", graph_path.display())))?;
    let nodes = raw
        .nodes
        .into_iter()
        .map(|(slug, n)| {
            (
                slug,
                AdjNode {
                    edges_in: n.edges_in,
                    edges_out: n.edges_out,
                },
            )
        })
        .collect();
    Ok(AdjIndex { nodes })
}

// ─── Инструмент ───────────────────────────────────────────────────────────

/// `concept_search`: типизированный поиск по базе концептов Ариадны.
struct ConceptSearchTool;

/// Аргументы инструмента `concept_search`.
#[derive(Deserialize)]
struct ConceptArgs {
    #[serde(default)]
    op: Option<String>,
    #[serde(default)]
    term: Option<String>,
    #[serde(default)]
    query: Option<String>,
    #[serde(default)]
    direction: Option<String>,
    #[serde(default, rename = "type")]
    kind: Option<String>,
    #[serde(default)]
    level: Option<String>,
    #[serde(default)]
    formality: Option<String>,
    #[serde(default)]
    family: Option<String>,
    #[serde(default)]
    sort: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
}

/// Инструменты домена: `concept_search` (регистрируется только при `[concept].enabled`).
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(ConceptSearchTool)]
}

#[async_trait]
impl Tool for ConceptSearchTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "concept_search".into(),
            description: "Типизированный поиск по базе концептов Ариадны (186K ML-концептов): op=resolve (термин→slug), lookup (по slug), neighbors (смежность), search (по слагу/заголовку с фасетами type/level/formality/family), stats.".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "op": {"type": "string", "description": "resolve|lookup|neighbors|search|stats (по умолчанию resolve)"},
                    "term": {"type": "string", "description": "Термин/slug для resolve/lookup/neighbors"},
                    "query": {"type": "string", "description": "Подстрока для search"},
                    "direction": {"type": "string", "description": "in|out|both (для neighbors, по умолчанию both)"},
                    "type": {"type": "string", "description": "Фасет: тип концепта"},
                    "level": {"type": "string", "description": "Фасет: уровень α|β|γ"},
                    "formality": {"type": "string", "description": "Фасет: формальность A|B|C"},
                    "family": {"type": "string", "description": "Фасет: семейство"},
                    "sort": {"type": "string", "description": "relevance|centrality|alpha (для search)"},
                    "limit": {"type": "integer", "description": "Число записей (по умолчанию 10, максимум 20)"}
                }
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: ConceptArgs = serde_json::from_value(args).map_err(|e| {
            HarnessError::Tool(format!("concept_search: невалидные аргументы: {e}"))
        })?;
        let index = &ctx.config.concept.index;
        if index.as_os_str().is_empty() {
            return Ok(ToolOutput::err(
                "concept_search: не задан [concept].index в конфиге — укажите путь к concept_index.json"
                    .to_string(),
            ));
        }
        let db = ConceptDb::open_cached(index)
            .map_err(|e| HarnessError::Tool(format!("concept_search: {e}")))?;
        let out = render(&db, &args)?;
        Ok(ToolOutput::ok(out))
    }
}

/// Рендерит результат операции в компактный текст.
fn render(db: &ConceptDb, args: &ConceptArgs) -> Result<String> {
    let op = args.op.as_deref().unwrap_or("resolve");
    match op {
        "stats" => {
            let s = db.stats();
            let mut o = format!(
                "концептов: {} (заявлено индексом: {}), алиасов: {}, узлов графа: {}",
                s.concepts,
                s.reported_total
                    .map_or_else(|| "—".into(), |n| n.to_string()),
                s.aliases,
                s.graph_nodes
            );
            if !s.by_type.is_empty() {
                let types = s
                    .by_type
                    .iter()
                    .map(|(k, v)| format!("{k}={v}"))
                    .collect::<Vec<_>>()
                    .join(", ");
                let _ = writeln!(o, "\nпо типам: {types}");
            }
            Ok(o)
        }
        "lookup" | "resolve" => {
            let term = args.term.as_deref().unwrap_or("");
            if term.is_empty() {
                return Ok("концепт не найден: пустой термин".to_string());
            }
            let (slug, kind) = if op == "lookup" {
                db.lookup(term)
                    .map(|c| (c.slug.clone(), "slug".to_string()))
                    .unwrap_or_default()
            } else {
                let r = db.resolve(term);
                let kind = match r.kind {
                    MatchKind::Slug => "slug",
                    MatchKind::AliasExact => "алиас (точный)",
                    MatchKind::AliasFolded => "алиас (свёрнутый)",
                    MatchKind::Fuzzy => "нечёткое",
                    MatchKind::None => "не найдено",
                }
                .to_string();
                (r.slug.unwrap_or_default(), kind)
            };
            if slug.is_empty() {
                return Ok(format!("концепт «{term}» не найден"));
            }
            match db.lookup(&slug) {
                Some(c) => Ok(format!(
                    "{slug} — {} [{}]\nуровень: {}, формальность: {}, семейство: {}\nфайл: {}",
                    c.title.as_deref().unwrap_or(NO_TITLE),
                    if c.kind.is_empty() { NO_KIND } else { &c.kind },
                    c.level.as_deref().unwrap_or("—"),
                    c.formality.as_deref().unwrap_or("—"),
                    c.family.as_deref().unwrap_or("—"),
                    c.file
                )),
                None => Ok(format!("{slug} (совпадение: {kind})")),
            }
        }
        "neighbors" => {
            let slug = args.term.as_deref().unwrap_or("");
            let dir = Direction::parse(args.direction.as_deref().unwrap_or("both"))?;
            let ns = db.neighbors(slug, dir);
            if ns.is_empty() {
                return Ok(format!("у «{slug}» нет соседей (или концепт не найден)"));
            }
            let lines = ns
                .iter()
                .map(|n| {
                    format!(
                        "- {} (in-degree {}): {}",
                        n.slug,
                        n.degree,
                        n.title.as_deref().unwrap_or(NO_TITLE)
                    )
                })
                .collect::<Vec<_>>()
                .join("\n");
            Ok(format!("соседи «{slug}» ({}):\n{lines}", dir.as_str()))
        }
        "search" => {
            let query = args.query.as_deref().unwrap_or("");
            let facets = Facets {
                kind: args.kind.clone(),
                level: args.level.clone(),
                formality: args.formality.clone(),
                family: args.family.clone(),
            };
            let sort = Sort::parse(args.sort.as_deref().unwrap_or("relevance"))?;
            let limit = args.limit.unwrap_or(DEFAULT_HITS);
            let hits = db.search(query, &facets, sort, limit);
            if hits.is_empty() {
                return Ok("ничего не найдено".to_string());
            }
            let lines = hits
                .iter()
                .map(|c| {
                    format!(
                        "- {}: {} [{}]",
                        c.slug,
                        c.title.as_deref().unwrap_or(NO_TITLE),
                        if c.kind.is_empty() { NO_KIND } else { &c.kind }
                    )
                })
                .collect::<Vec<_>>()
                .join("\n");
            Ok(format!("найдено {}:\n{lines}", hits.len()))
        }
        other => Ok(format!(
            "неизвестная операция «{other}»: ожидалось resolve|lookup|neighbors|search|stats"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_db() -> (ConceptDb, tempfile::TempDir) {
        let dir = tempfile::tempdir().expect("tempdir");
        let index = dir.path().join("concept_index.json");
        let graph = dir.path().join("concept_graph.json");
        std::fs::write(&index, r#"{
            "concept_index": {
                "concepts": {
                    "grpo": {"type": "algorithmic_primitive", "file": "concepts/algorithmic_primitive/grpo.md", "title": "GRPO", "level": "\u03b2", "formality": "B", "family": "RL Algorithms"},
                    "attention": {"type": "architectural_component", "file": "concepts/architectural_component/attention.md", "title": "Attention", "level": "\u03b1", "formality": "A", "family": "Transformers"}
                },
                "aliases": {"group_relative_policy_optimization": "grpo", "Self-Attention": "attention"}
            },
            "stats": {"total_concepts": 2, "by_type": {"algorithmic_primitive": 1, "architectural_component": 1}}
        }"#).expect("write index");
        std::fs::write(&graph, r#"{"nodes": {"grpo": {"edges_in": ["attention"], "edges_out": []}, "attention": {"edges_in": ["transformer"], "edges_out": ["grpo"]}, "transformer": {"edges_in": [], "edges_out": ["attention"]}}, "edges": []}"#).expect("write graph");
        let db = ConceptDb::load_paths(&index, &graph).expect("load");
        (db, dir)
    }

    #[test]
    fn resolves_alias_and_lookup() {
        let (db, _dir) = fixture_db();
        let r = db.resolve("group_relative_policy_optimization");
        assert_eq!(r.slug.as_deref(), Some("grpo"));
        assert_eq!(r.kind, MatchKind::AliasExact);
        let c = db.lookup("attention").expect("lookup");
        assert_eq!(c.title.as_deref(), Some("Attention"));
    }

    #[test]
    fn neighbors_and_search() {
        let (db, _dir) = fixture_db();
        let ns = db.neighbors("grpo", Direction::In);
        assert_eq!(ns.len(), 1);
        assert_eq!(ns[0].slug, "attention");
        assert_eq!(ns[0].degree, 1);
        let hits = db.search("atten", &Facets::default(), Sort::Alpha, 10);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].slug, "attention");
    }

    #[test]
    fn fuzzy_floor_keeps_garbage_out_but_catches_typos() {
        // Короткий slug (≤3) не участвует в нечётком: «xyzzy» не должно
        // ловиться в «xyz»; опечатка в длинном термине — ловится.
        let dir = tempfile::tempdir().expect("tempdir");
        let index = dir.path().join("concept_index.json");
        let graph = dir.path().join("concept_graph.json");
        std::fs::write(&index, r#"{
            "concept_index": {
                "concepts": {
                    "attention": {"type": "architectural_component", "file": "attention.md", "title": "Attention", "level": "α", "formality": "A", "family": "Transformers"},
                    "xyz": {"type": "misc", "file": "xyz.md", "title": "XYZ", "level": "γ", "formality": "C", "family": "Misc"}
                },
                "aliases": {}
            },
            "stats": {"total_concepts": 2}
        }"#).expect("write index");
        std::fs::write(&graph, r#"{"nodes": {}, "edges": []}"#).expect("write graph");
        let db = ConceptDb::load_paths(&index, &graph).expect("load");
        let garbage = db.resolve("xyzzy");
        assert_eq!(garbage.slug, None, "мусорное слово обязано быть unknown");
        let typo = db.resolve("attentio");
        assert_eq!(typo.slug.as_deref(), Some("attention"));
        assert_eq!(typo.kind, MatchKind::Fuzzy);
    }

    #[test]
    #[ignore = "живой прогон: требует ~/library/index/concept_index.json"]
    fn live_real_index_loads_and_resolves() {
        let home = std::env::var_os("HOME").expect("HOME");
        let index = std::path::PathBuf::from(home).join("library/index/concept_index.json");
        if !index.exists() {
            eprintln!("[SKIP] нет {}", index.display());
            return;
        }
        let db = ConceptDb::load(&index).expect("загрузка реального индекса");
        assert!(db.len() > 100_000, "карточек: {}", db.len());
        let r = db.resolve("GRPO");
        assert!(r.slug.is_some(), "GRPO не резолвится");
        let slug = r.slug.clone().expect("slug");
        let ns = db.neighbors(&slug, Direction::In);
        assert!(!ns.is_empty(), "у GRPO нет входящих рёбер");
        eprintln!(
            "OK: карточек={}, GRPO->{slug} ({:?}), входящих соседей={}",
            db.len(),
            r.kind,
            ns.len()
        );
    }
}
