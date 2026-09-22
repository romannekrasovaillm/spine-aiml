//! Локальная база знаний архитектора: поиск по файловой системе (v2).
//!
//! КОНТРАКТ (владелец: агент `web` — общий с web.rs):
//! - [`search`] / [`search_with`] — обходят `KnowledgeConfig::dirs` (walkdir),
//!   фильтруют по расширениям, ранжируют BM25-подобным скорингом поверх
//!   кэша корпуса в памяти; возвращают хиты со сниппетом (±2 строки
//!   контекста, подсветка маркерами `>>>`) и breadcrumb раздела для md;
//! - файлы >5 МБ индексируются только по имени; битый UTF-8 — lossy;
//!   скрытые каталоги и `target/.git/node_modules` пропускаются;
//! - поиск по именам файлов тоже (отдельный канал скоринга; режим
//!   [`SearchOptions::names_only`] — только этот канал);
//! - словоформы без словарей: префиксный/корневой матч токенов для терминов
//!   от 5 символов; при полном промахе — триграммный фолбэк против опечаток
//!   (хиты помечаются «нечёткое совпадение»).

use std::collections::{HashMap, HashSet};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock, RwLock};
use std::time::SystemTime;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use walkdir::WalkDir;

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Файлы больше этого размера индексируются только по имени.
const MAX_CONTENT_BYTES: u64 = 5 * 1024 * 1024;
/// Строк контекста вокруг совпадения в сниппете.
const CONTEXT_LINES: usize = 2;
/// Максимальная длина строки сниппета (символов).
const MAX_SNIPPET_LINE: usize = 240;
/// Суммарный объём кэша корпуса (байт содержимого; индексы строк/токенов
/// оцениваются этой же величиной с коэффициентом ~2). При превышении —
/// LRU-выселение целых файлов.
const CACHE_MAX_BYTES: usize = 256 * 1024 * 1024;
/// BM25: насыщение термин-частоты (k1). Больше — длиннее «хвост» влияния
/// повторов термина.
const BM25_K1: f64 = 1.5;
/// BM25: нормализация по длине документа (b). 0 — длина игнорируется,
/// 1 — полная нормализация.
const BM25_B: f64 = 0.75;
/// Минимальная длина термина (символов) для префиксного/корневого матча.
/// Короткие термины («фз», «api») матчатся только точным токеном или
/// подстрокой, чтобы не взрывать префиксный матч.
const PREFIX_MIN_TERM_CHARS: usize = 5;
/// Максимум терминов запроса, участвующих в матчинге (битовые маски строк —
/// `u64`, лишние термины отсекаются после сортировки).
const MAX_QUERY_TERMS: usize = 32;
/// Множитель веса канала имени файла (к idf термина).
const NAME_BONUS: f64 = 2.5;
/// Множитель веса совпадения в строке-заголовке markdown (`#`…).
const HEADING_BOOST: f64 = 3.0;
/// Бонус за все термины запроса в одной строке (множитель к среднему idf).
const PHRASE_LINE_BONUS: f64 = 3.0;
/// Бонус за все термины запроса в окне ±[`PHRASE_WINDOW_LINES`] строк.
const PHRASE_WINDOW_BONUS: f64 = 1.5;
/// Радиус окна фразового бонуса (строк).
const PHRASE_WINDOW_LINES: usize = 3;
/// Порог триграммного сходства (Jaccard над char-3-grams) для нечёткого
/// фолбэка против опечаток.
const FUZZY_TRIGRAM_THRESHOLD: f64 = 0.45;
/// Штраф нечёткого совпадения к весу tf (фолбэк слабее точного матча).
const FUZZY_SCORE_WEIGHT: f64 = 0.5;
/// Максимум хитов на один файл в топе (разнообразие выдачи). Если хитов
/// не хватает до лимита — ограничение ослабляется (добор из остатка).
const MAX_HITS_PER_FILE: usize = 2;

/// Хит локального поиска.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KbHit {
    /// Путь к файлу.
    pub path: PathBuf,
    /// Номер строки совпадения (1-based; 0 — совпадение по имени файла).
    pub line: usize,
    /// Оценка релевантности (больше — лучше).
    pub score: f64,
    /// Сниппет с контекстом.
    pub snippet: String,
    /// Ближайший заголовок markdown выше строки совпадения (пусто для не-md).
    #[serde(default)]
    pub breadcrumb: String,
    /// Хит получен нечётким (триграммным) фолбэком против опечатки.
    #[serde(default)]
    pub fuzzy: bool,
    /// Паспорт файла из YAML-frontmatter (`None` — паспорта нет либо он без фасетов).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub facets: Option<FrontmatterFacets>,
}

/// Паспорт документа из YAML-frontmatter (см. концепт-фронтматтер-в-markdown).
///
/// Все поля — сырые строки: kb не валидатор библиотеки, он фасетирует.
/// Разбор терпимый: неизвестный класс документа фасетируется как есть.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct FrontmatterFacets {
    /// Класс документа: `concept` | `digest` | `synthesis` | `playbook` | `skill`.
    #[serde(default)]
    pub r#type: Option<String>,
    /// Гейт качества: `draft` (написано агентом) | `stable` (прошло ревью).
    #[serde(default)]
    pub status: Option<String>,
    /// Provenance: `arXiv:2501.12345`, `диалог 2026-08-04`.
    #[serde(default)]
    pub source: Option<String>,
    /// Теги-концепты (kebab-case, 3–7).
    #[serde(default)]
    pub concepts: Vec<String>,
    /// Дата создания `YYYY-MM-DD` (строка: терпимо к частичным датам).
    #[serde(default)]
    pub created: Option<String>,
    /// Число применений playbook (P1, зарезервировано).
    #[serde(default)]
    pub uses: Option<u32>,
    /// Пометка «кандидат в скилл» (P1, зарезервировано).
    #[serde(default)]
    pub skill_candidate: Option<bool>,
}

impl FrontmatterFacets {
    /// Паспорта нет вовсе: все скаляры пусты, список концептов пуст.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.r#type.is_none()
            && self.status.is_none()
            && self.source.is_none()
            && self.concepts.is_empty()
            && self.created.is_none()
            && self.uses.is_none()
            && self.skill_candidate.is_none()
    }
}

/// Фильтр по паспортам. Внутри поля — OR, между полями — AND.
///
/// Пустой список = поле не ограничивает. Файл без frontmatter не проходит
/// ни один непустой фильтр (строгий режим — фильтр означает «только размеченные»).
/// Значения полей должны быть нормализованы (lowercase/trim) — так их строит
/// инструмент `kb_search` из аргументов.
#[derive(Debug, Clone, Default)]
pub struct FacetFilter {
    /// `type ∈ …`.
    pub types: Vec<String>,
    /// `status ∈ …`.
    pub statuses: Vec<String>,
    /// `concepts ∩ … ≠ ∅`.
    pub concepts: Vec<String>,
}

impl FacetFilter {
    /// Пустой фильтр — ограничений нет.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.types.is_empty() && self.statuses.is_empty() && self.concepts.is_empty()
    }
}

/// Опции одного вызова поиска (расширение контракта `kb_search` v2).
#[derive(Debug, Clone, Default)]
pub struct SearchOptions {
    /// Подстрока или простой glob (`*`, `?`) по относительному пути файла
    /// (относительно корня базы знаний). `*` матчится и через `/`.
    pub path_filter: Option<String>,
    /// Переопределение списка расширений конфига на этот вызов.
    pub extensions: Option<Vec<String>>,
    /// Режим «только имена»: содержимое файлов не читается, матч — только
    /// по имени (быстрый поиск «где лежит файл про …»).
    pub names_only: bool,
    /// Фасетный фильтр по frontmatter (пустой — без фильтра, см. [`FacetFilter`]).
    pub facets: FacetFilter,
}

/// Поиск по локальной базе знаний.
///
/// Обход выполняется в `spawn_blocking`, чтобы не блокировать runtime.
/// Недоступные каталоги и нечитаемые файлы пропускаются.
///
/// # Errors
/// Фоновой обход прерван (`JoinError`).
pub async fn search(
    dirs: &[PathBuf],
    exts: &[String],
    query: &str,
    limit: usize,
) -> Result<Vec<KbHit>> {
    search_with(dirs, exts, query, limit, &SearchOptions::default()).await
}

/// Поиск по локальной базе знаний с опциями вызова ([`SearchOptions`]).
///
/// # Errors
/// Фоновой обход прерван (`JoinError`).
pub async fn search_with(
    dirs: &[PathBuf],
    exts: &[String],
    query: &str,
    limit: usize,
    options: &SearchOptions,
) -> Result<Vec<KbHit>> {
    let dirs = dirs.to_vec();
    let exts = exts.to_vec();
    let query = query.to_string();
    let options = options.clone();
    tokio::task::spawn_blocking(move || search_blocking(&dirs, &exts, &query, limit, &options))
        .await
        .map_err(|e| HarnessError::Kb(format!("обход базы знаний прерван: {e}")))
}

/// Инструменты домена: `kb_search`.
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(KbSearchTool)]
}

/// Ключ кэша: канонизированный набор каталогов + расширений корпуса.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct CorpusKey {
    /// Каталоги (отсортированы, дедуплицированы).
    dirs: Vec<PathBuf>,
    /// Расширения (нормализованы: без точки, lowercase, отсортированы).
    exts: Vec<String>,
}

/// Индекс одной строки файла в кэше.
#[derive(Debug)]
struct LineIndex {
    /// Начало байт-диапазона строки в содержимом.
    start: usize,
    /// Конец байт-диапазона строки (без перевода строки).
    end: usize,
    /// Токены строки (lowercase, разбивка по не-буквам/цифрам).
    tokens: Vec<String>,
}

/// Кэшированный файл корпуса.
#[derive(Debug)]
struct CachedFile {
    /// mtime на момент индексации (инвалидация по паре mtime+len).
    mtime: Option<SystemTime>,
    /// Размер на момент индексации.
    len: u64,
    /// Индекс заголовков построен (md).
    has_headings: bool,
    /// Содержимое (`None`: файл > [`MAX_CONTENT_BYTES`] либо запись создана
    /// в режиме «только имена» — дочитается при первом контентном запросе).
    content: Option<Arc<str>>,
    /// Предраспарсенные строки (пусто без содержимого).
    lines: Vec<LineIndex>,
    /// Индекс заголовков md: (индекс строки, текст заголовка).
    headings: Vec<(usize, String)>,
    /// Разобранный паспорт (`None` — frontmatter отсутствует или битый).
    frontmatter: Option<FrontmatterFacets>,
    /// Индекс строки, с которой начинается тело (0 — паспорта нет).
    /// Строки `< body_start` исключены из контентного скоринга.
    body_start: usize,
    /// Длина документа в токенах (для нормализации BM25).
    doc_tokens: usize,
    /// Тик LRU (монотонный счётчик обращений).
    tick: u64,
}

impl CachedFile {
    /// Запись без содержимого (большой файл или режим «только имена»).
    fn name_only(mtime: Option<SystemTime>, len: u64) -> Self {
        Self {
            mtime,
            len,
            has_headings: false,
            content: None,
            lines: Vec::new(),
            headings: Vec::new(),
            frontmatter: None,
            body_start: 0,
            doc_tokens: 0,
            tick: 0,
        }
    }

    /// Проходит ли файл фасетный фильтр ([`FacetFilter`]: OR внутри поля,
    /// AND между полями). Файл без паспорта пропускает только пустой фильтр.
    fn facets_match(&self, filter: &FacetFilter) -> bool {
        let Some(fm) = &self.frontmatter else {
            return filter.is_empty();
        };
        let type_ok = filter.types.is_empty()
            || fm
                .r#type
                .as_deref()
                .is_some_and(|t| filter.types.iter().any(|x| x == t));
        let status_ok = filter.statuses.is_empty()
            || fm
                .status
                .as_deref()
                .is_some_and(|s| filter.statuses.iter().any(|x| x == s));
        let concepts_ok = filter.concepts.is_empty()
            || fm
                .concepts
                .iter()
                .any(|c| filter.concepts.iter().any(|x| x == c));
        type_ok && status_ok && concepts_ok
    }

    /// Учитываемый в бюджете кэша объём записи (байты содержимого).
    fn accounted_bytes(&self) -> usize {
        self.content.as_ref().map_or(0, |c| c.len())
    }
}

/// Кэш одного корпуса (набор файлов + бюджет + LRU-тик).
#[derive(Debug, Default)]
struct CorpusCache {
    /// Файлы по абсолютному пути.
    files: HashMap<PathBuf, CachedFile>,
    /// Суммарный учитываемый объём (байты содержимого).
    total_bytes: usize,
    /// Монотонный счётчик для LRU-тиков.
    tick: u64,
}

impl CorpusCache {
    /// Следующий LRU-тик.
    fn next_tick(&mut self) -> u64 {
        self.tick += 1;
        self.tick
    }
}

/// Глобальный кэш корпусов: ключ — набор dirs+exts.
static CACHE: OnceLock<RwLock<HashMap<CorpusKey, CorpusCache>>> = OnceLock::new();

/// Доступ к глобальному кэшу (ленивая инициализация).
fn cache() -> &'static RwLock<HashMap<CorpusKey, CorpusCache>> {
    CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Тестовый хук: пути файлов, реально прочитанных с диска (проверка
/// отсутствия повторного чтения при горячем кэше). Тесты фильтруют журнал
/// по своему каталогу, поэтому параллельный прогон не мешает.
#[cfg(test)]
static READ_LOG: std::sync::Mutex<Vec<PathBuf>> = std::sync::Mutex::new(Vec::new());

/// Режим матчинга термина к токену документа.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MatchMode {
    /// Точный токен / подстрока (короткие термины) или корневой матч (длинные).
    Stem,
    /// Нечёткий фолбэк: триграммное сходство против опечаток.
    Fuzzy,
}

/// Синхронный обход и ранжирование (вызывается из `spawn_blocking`).
fn search_blocking(
    dirs: &[PathBuf],
    exts: &[String],
    query: &str,
    limit: usize,
    options: &SearchOptions,
) -> Vec<KbHit> {
    let terms = query_terms(query);
    if terms.is_empty() {
        return Vec::new();
    }
    let exts_norm = normalize_exts(options.extensions.as_deref().unwrap_or(exts));
    let key = CorpusKey {
        dirs: sorted_dirs(dirs),
        exts: exts_norm.clone(),
    };

    // Отравление RwLock не страшно: записи кэша самосогласованы.
    let mut guard = match cache().write() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    let corpus = guard.entry(key).or_default();

    // 1. Обход ФС (дешёвый: только metadata) + дочитывание изменившихся файлов.
    let candidates = collect_candidates(dirs, &exts_norm, options.path_filter.as_deref());
    for path in &candidates {
        refresh_file(corpus, path, !options.names_only);
    }
    evict_if_needed(corpus);

    // 1a. Фасетный префилт — до скоринга: df/n_docs/avgdl считаются по суженому
    //     корпусу, иначе idf «размывается» документами, которые всё равно
    //     отфильтруются. Один префилт на оба прохода (Stem и Fuzzy).
    let filtered: Vec<PathBuf> = if options.facets.is_empty() {
        candidates
    } else {
        candidates
            .into_iter()
            .filter(|p| {
                corpus
                    .files
                    .get(p)
                    .is_some_and(|f| f.facets_match(&options.facets))
            })
            .collect()
    };

    // 2. Точный/корневой проход; при полном промахе — триграммный фолбэк.
    let mut hits = score_corpus(corpus, &filtered, &terms, options, MatchMode::Stem);
    if hits.is_empty() {
        hits = score_corpus(corpus, &filtered, &terms, options, MatchMode::Fuzzy);
    }

    // 3. Сортировка, дедупликация, разнообразие топа.
    hits.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.path.cmp(&b.path))
            .then_with(|| a.line.cmp(&b.line))
    });
    let mut seen = HashSet::new();
    hits.retain(|h| seen.insert((h.path.clone(), h.line)));
    diversify(hits, limit)
}

/// Термины запроса: разбивка по пробелам, lowercase, дедупликация, не более
/// [`MAX_QUERY_TERMS`] (битовые маски строк — `u64`).
fn query_terms(query: &str) -> Vec<String> {
    let mut terms: Vec<String> = query.split_whitespace().map(str::to_lowercase).collect();
    terms.sort();
    terms.dedup();
    terms.truncate(MAX_QUERY_TERMS);
    terms
}

/// Нормализация списка расширений: без точки, lowercase, отсортированы.
fn normalize_exts(exts: &[String]) -> Vec<String> {
    let mut out: Vec<String> = exts
        .iter()
        .map(|e| e.trim_start_matches('.').to_lowercase())
        .collect();
    out.sort();
    out.dedup();
    out
}

/// Канонизация каталогов ключа кэша: сортировка + дедупликация.
fn sorted_dirs(dirs: &[PathBuf]) -> Vec<PathBuf> {
    let mut out = dirs.to_vec();
    out.sort();
    out.dedup();
    out
}

/// Список файлов-кандидатов: обход каталогов с фильтрами расширения и пути.
fn collect_candidates(
    dirs: &[PathBuf],
    exts: &[String],
    path_filter: Option<&str>,
) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for dir in dirs {
        let walker = WalkDir::new(dir)
            .follow_links(false)
            .into_iter()
            .filter_entry(is_searchable);
        for entry in walker.filter_map(std::result::Result::ok) {
            if !entry.file_type().is_file() {
                continue;
            }
            let path = entry.path();
            if !exts.is_empty() && !has_allowed_extension(path, exts) {
                continue;
            }
            if let Some(filter) = path_filter {
                let rel = path.strip_prefix(dir).unwrap_or(path);
                if !path_filter_matches(filter, rel) {
                    continue;
                }
            }
            out.push(path.to_path_buf());
        }
    }
    out.sort();
    out
}

/// Пропускает скрытые каталоги/файлы и служебные каталоги сборки.
fn is_searchable(entry: &walkdir::DirEntry) -> bool {
    if entry.depth() == 0 {
        return true;
    }
    let name = entry.file_name().to_string_lossy();
    !name.starts_with('.') && name != "target" && name != "node_modules"
}

/// Расширение файла входит в список допустимых (сравнение без учёта регистра).
fn has_allowed_extension(path: &Path, exts: &[String]) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| exts.iter().any(|x| x.eq_ignore_ascii_case(e)))
}

/// Markdown-файл (по расширению).
fn is_markdown_path(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("md") || e.eq_ignore_ascii_case("markdown"))
}

/// Фильтр по относительному пути: подстрока или простой glob (`*`, `?`).
fn path_filter_matches(filter: &str, rel: &Path) -> bool {
    let text = rel.to_string_lossy().replace('\\', "/");
    if filter.contains('*') || filter.contains('?') {
        glob_match(filter, &text)
    } else {
        text.contains(filter)
    }
}

/// Простейший glob: `*` — любая последовательность (включая `/`),
/// `?` — ровно один символ. Двухуказательный алгоритм с откатом к `*`.
fn glob_match(pattern: &str, text: &str) -> bool {
    let pat_chars: Vec<char> = pattern.chars().collect();
    let text_chars: Vec<char> = text.chars().collect();
    let (mut pat_idx, mut txt_idx) = (0usize, 0usize);
    let (mut star_pat, mut star_txt) = (usize::MAX, 0usize);
    while txt_idx < text_chars.len() {
        if pat_idx < pat_chars.len()
            && (pat_chars[pat_idx] == '?' || pat_chars[pat_idx] == text_chars[txt_idx])
        {
            pat_idx += 1;
            txt_idx += 1;
        } else if pat_idx < pat_chars.len() && pat_chars[pat_idx] == '*' {
            star_pat = pat_idx;
            star_txt = txt_idx;
            pat_idx += 1;
        } else if star_pat != usize::MAX {
            pat_idx = star_pat + 1;
            star_txt += 1;
            txt_idx = star_txt;
        } else {
            return false;
        }
    }
    while pat_idx < pat_chars.len() && pat_chars[pat_idx] == '*' {
        pat_idx += 1;
    }
    pat_idx == pat_chars.len()
}

/// Обновляет запись кэша для файла: перечитывает только при смене
/// (mtime, len) либо когда нужно содержимое, а запись — «только имя».
fn refresh_file(corpus: &mut CorpusCache, path: &Path, read_content: bool) {
    let Ok(meta) = std::fs::metadata(path) else {
        return;
    };
    let len = meta.len();
    let mtime = meta.modified().ok();
    let fresh = corpus.files.get(path).is_some_and(|entry| {
        entry.mtime == mtime && entry.len == len && (entry.content.is_some() || !read_content)
    });
    if fresh {
        let tick = corpus.next_tick();
        if let Some(entry) = corpus.files.get_mut(path) {
            entry.tick = tick;
        }
        return;
    }
    let mut entry = if read_content && len <= MAX_CONTENT_BYTES {
        let Some(parsed) = parse_file(path, mtime, len) else {
            return;
        };
        parsed
    } else {
        CachedFile::name_only(mtime, len)
    };
    entry.tick = corpus.next_tick();
    corpus.total_bytes += entry.accounted_bytes();
    if let Some(old) = corpus.files.insert(path.to_path_buf(), entry) {
        corpus.total_bytes = corpus.total_bytes.saturating_sub(old.accounted_bytes());
    }
}

/// Читает и разбирает файл: содержимое, строки с токенами, заголовки md.
fn parse_file(path: &Path, mtime: Option<SystemTime>, len: u64) -> Option<CachedFile> {
    let bytes = std::fs::read(path).ok()?;
    #[cfg(test)]
    {
        // Игнорируем отравление мьютекса тестового журнала: потеря записи
        // в тестовом хуке не влияет на продуктовый код.
        if let Ok(mut log) = READ_LOG.lock() {
            log.push(path.to_path_buf());
        }
    }
    let text: Arc<str> = String::from_utf8_lossy(&bytes).into_owned().into();
    let markdown = is_markdown_path(path);
    let (frontmatter, body_start) = if markdown {
        scan_frontmatter(&text)
    } else {
        (None, 0)
    };
    let mut lines = Vec::new();
    let mut headings = Vec::new();
    let mut doc_tokens = 0usize;
    let mut offset = 0usize;
    // split_inclusive даёт точные байт-смещения строк (включая CRLF).
    for (idx, raw) in text.split_inclusive('\n').enumerate() {
        let line = raw.trim_end_matches(['\n', '\r']);
        // Паспорт — не тело: его строки не идут ни в doc_tokens, ни в токены
        // строк (нулевая маска ⇒ не могут стать «лучшей строкой» хита).
        if idx < body_start {
            lines.push(LineIndex {
                start: offset,
                end: offset + line.len(),
                tokens: Vec::new(),
            });
            offset += raw.len();
            continue;
        }
        let tokens = tokenize(line);
        doc_tokens += tokens.len();
        if markdown {
            if let Some(title) = heading_title(line) {
                headings.push((idx, title));
            }
        }
        lines.push(LineIndex {
            start: offset,
            end: offset + line.len(),
            tokens,
        });
        offset += raw.len();
    }
    Some(CachedFile {
        mtime,
        len,
        has_headings: !headings.is_empty(),
        content: Some(text),
        lines,
        headings,
        frontmatter,
        body_start,
        doc_tokens,
        tick: 0,
    })
}

/// Разбирает YAML-frontmatter в начале текста: `(паспорт, body_start)`.
///
/// Терпимый контракт: битый YAML не ошибка — фасетов нет, но `body_start` всё
/// равно указывает за закрывающий `---`, чтобы паспорт не тёк в контентный
/// скоринг. Незакрытый блок (нет второго `---`) паспортом не считается:
/// `(None, 0)` — файл индексируется целиком.
///
/// Допускается ведущий HTML-комментарий (`<!-- filename: … -->`) и BOM —
/// методология: для агентов порядок «комментарий → frontmatter» безразличен.
/// Берётся **первый** блок от начала файла (дублированный паспорт карточек
/// концептов остаётся телом).
fn scan_frontmatter(text: &str) -> (Option<FrontmatterFacets>, usize) {
    let raw_lines: Vec<&str> = text.split_inclusive('\n').collect();
    let clean = |i: usize| {
        raw_lines
            .get(i)
            .copied()
            .unwrap_or_default()
            .trim_end_matches(['\n', '\r'])
            .trim_start_matches('\u{feff}')
    };

    // Пропускаем BOM, пустые строки и ведущие HTML-комментарии.
    let mut idx = 0usize;
    while idx < raw_lines.len() {
        let line = clean(idx).trim_start();
        if line.is_empty() {
            idx += 1;
            continue;
        }
        if line.starts_with("<!--") {
            idx += 1;
            if !line.contains("-->") {
                while idx < raw_lines.len() {
                    let done = clean(idx).contains("-->");
                    idx += 1;
                    if done {
                        break;
                    }
                }
            }
            continue;
        }
        break;
    }

    if idx >= raw_lines.len() || clean(idx).trim() != "---" {
        return (None, 0);
    }
    let open_idx = idx;
    idx += 1;
    let mut close_idx = None;
    while idx < raw_lines.len() {
        if clean(idx).trim() == "---" {
            close_idx = Some(idx);
            break;
        }
        idx += 1;
    }
    let Some(close) = close_idx else {
        return (None, 0);
    };
    let body_start = close + 1;
    let yaml: String = (open_idx + 1..close)
        .map(clean)
        .collect::<Vec<_>>()
        .join("\n");

    let parsed = match parse_facets_yaml(&yaml) {
        Some(mut facets) => {
            normalize_facets(&mut facets);
            if facets.is_empty() {
                None
            } else {
                Some(facets)
            }
        }
        // Построчный фолбэк: модель/люди пишут `description: Текст: с двоеточием` —
        // это невалидный YAML, но нужные фасеты извлекаются до конца строки.
        None => scan_frontmatter_scalars(&yaml),
    };
    (parsed, body_start)
}

/// Строгий разбор паспорта через `serde_yaml_ng` с нормализацией регистра
/// ключей (`Type` ≡ `type` — спека, §4.4.6).
///
/// `None` — YAML не разобран, корень не отображение либо значение поля имеет
/// несовместимый тип (напр. `concepts: dpo` строкой): вызывающий уходит в
/// построчный фолбэк [`scan_frontmatter_scalars`].
fn parse_facets_yaml(yaml: &str) -> Option<FrontmatterFacets> {
    let value: serde_yaml_ng::Value = serde_yaml_ng::from_str(yaml).ok()?;
    let serde_yaml_ng::Value::Mapping(map) = value else {
        return None;
    };
    let mut normalized = serde_yaml_ng::Mapping::new();
    for (key, item) in map {
        if let serde_yaml_ng::Value::String(name) = key {
            normalized.insert(
                serde_yaml_ng::Value::String(name.trim().to_lowercase()),
                item,
            );
        }
    }
    serde_yaml_ng::from_value::<FrontmatterFacets>(serde_yaml_ng::Value::Mapping(normalized)).ok()
}

/// Построчный фолбэк разбора паспорта (см. `plugin.rs::parse_frontmatter`).
/// Ключи `type`/`status`/`source`/`created` — значение до конца строки;
/// `concepts` — flow-список `[a, b]`, строка `a, b` или блок `- a`.
fn scan_frontmatter_scalars(block: &str) -> Option<FrontmatterFacets> {
    let mut facets = FrontmatterFacets::default();
    let mut concepts_open = false;
    for raw in block.lines() {
        let line = raw.trim_end_matches(['\r', '\n']);
        if line.trim().is_empty() {
            continue;
        }
        if concepts_open {
            if let Some(item) = line.trim_start().strip_prefix("- ") {
                push_concept(&mut facets.concepts, item);
                continue;
            }
            concepts_open = false;
        }
        // Продолжения folded/block-значений и вложенные списки не разбираем.
        if line.starts_with(' ') || line.starts_with('\t') {
            continue;
        }
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        let value = value.trim();
        match key.trim().to_lowercase().as_str() {
            "type" => facets.r#type = scalar_value(value),
            "status" => facets.status = scalar_value(value),
            "source" => facets.source = scalar_value(value),
            "created" => facets.created = scalar_value(value),
            "concepts" | "concept" | "tags" => {
                facets.concepts = concept_list(value);
                concepts_open = facets.concepts.is_empty();
            }
            _ => {}
        }
    }
    normalize_facets(&mut facets);
    if facets.is_empty() {
        None
    } else {
        Some(facets)
    }
}

/// Скалярное значение паспорта: снять кавычки, пустое — как `None`.
fn scalar_value(value: &str) -> Option<String> {
    let v = value.trim().trim_matches('"').trim_matches('\'').trim();
    if v.is_empty() {
        None
    } else {
        Some(v.to_string())
    }
}

/// Список концептов из значения `concepts`: flow-список `[a, b]`, `a, b`
/// или одиночный тег `dpo` (пустая строка — пустой список, вызывающий
/// переключается на сбор индентного блока `- a`).
fn concept_list(value: &str) -> Vec<String> {
    let inner = value
        .strip_prefix('[')
        .and_then(|v| v.strip_suffix(']'))
        .unwrap_or(value);
    let mut out = Vec::new();
    for item in inner.split(',') {
        push_concept(&mut out, item);
    }
    out
}

/// Добавляет тег в список концептов (trim, кавычки, пустые — отброшены).
fn push_concept(out: &mut Vec<String>, item: &str) {
    let tag = item.trim().trim_matches('"').trim_matches('\'').trim();
    if !tag.is_empty() {
        out.push(tag.to_string());
    }
}

/// Нормализация фасетов: регистр/пробелы. kb не валидатор — словарь классов
/// не проверяет, неизвестный `type` фасетируется как есть.
fn normalize_facets(facets: &mut FrontmatterFacets) {
    for field in [&mut facets.r#type, &mut facets.status] {
        if let Some(value) = field {
            let norm = value.trim().to_lowercase();
            if norm.is_empty() {
                *field = None;
            } else {
                *field = Some(norm);
            }
        }
    }
    for tag in &mut facets.concepts {
        *tag = tag.trim().to_lowercase();
    }
    facets.concepts.retain(|t| !t.is_empty());
    let mut seen = HashSet::new();
    facets.concepts.retain(|t| seen.insert(t.clone()));
    for field in [&mut facets.source, &mut facets.created] {
        if let Some(value) = field {
            let norm = value.trim().to_string();
            if norm.is_empty() {
                *field = None;
            } else {
                *field = Some(norm);
            }
        }
    }
}

/// Текст заголовка markdown (`# Заголовок` → `Заголовок`), иначе `None`.
fn heading_title(line: &str) -> Option<String> {
    let line = line.trim_start();
    let hashes = line.chars().take_while(|&c| c == '#').count();
    if hashes == 0 || hashes > 6 {
        return None;
    }
    // '#' — ASCII, срез по количеству символов безопасен.
    let title = line[hashes..].trim();
    if title.is_empty() {
        return None;
    }
    Some(title.trim_end_matches('#').trim_end().to_string())
}

/// Разбивает текст на токены (последовательности букв/цифр), lowercase.
fn tokenize(text: &str) -> Vec<String> {
    text.split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .map(str::to_lowercase)
        .collect()
}

/// Совпадение термина с токеном документа в заданном режиме.
fn term_matches_token(mode: MatchMode, term: &str, term_chars: usize, token: &str) -> bool {
    match mode {
        MatchMode::Stem => stem_matches(term, term_chars, token),
        MatchMode::Fuzzy => fuzzy_matches(term, term_chars, token),
    }
}

/// Точный/корневой матч без словарей: короткие термины — точный токен или
/// подстрока; длинные (от [`PREFIX_MIN_TERM_CHARS`]) — токен начинается с
/// термина, термин с токена-основы, либо общий корень ≥ половины меньшего
/// («идемпотентность» ловит «идемпотентный»/«идемпотентного»).
fn stem_matches(term: &str, term_chars: usize, token: &str) -> bool {
    if term_chars < PREFIX_MIN_TERM_CHARS {
        return token == term || token.contains(term);
    }
    let token_chars = token.chars().count();
    if token.starts_with(term) {
        return true;
    }
    if token_chars >= PREFIX_MIN_TERM_CHARS && term.starts_with(token) {
        return true;
    }
    let common = term
        .chars()
        .zip(token.chars())
        .take_while(|(a, b)| a == b)
        .count();
    common >= PREFIX_MIN_TERM_CHARS && common * 2 >= term_chars.min(token_chars)
}

/// Нечёткий матч: длины близки (±3 символа) и триграммное сходство выше
/// порога [`FUZZY_TRIGRAM_THRESHOLD`].
fn fuzzy_matches(term: &str, term_chars: usize, token: &str) -> bool {
    let token_chars = token.chars().count();
    if term_chars.abs_diff(token_chars) > 3 {
        return false;
    }
    trigram_jaccard(term, token) >= FUZZY_TRIGRAM_THRESHOLD
}

/// Множество char-триграмм строки (скользящее окно в 3 символа).
fn trigram_set(text: &str) -> HashSet<[char; 3]> {
    let chars: Vec<char> = text.chars().collect();
    chars.windows(3).map(|w| [w[0], w[1], w[2]]).collect()
}

/// Сходство Жаккара над множествами триграмм двух строк (0.0–1.0).
fn trigram_jaccard(left: &str, right: &str) -> f64 {
    let set_left = trigram_set(left);
    let set_right = trigram_set(right);
    if set_left.is_empty() || set_right.is_empty() {
        return 0.0;
    }
    let intersection = set_left.intersection(&set_right).count();
    let union = set_left.len() + set_right.len() - intersection;
    intersection as f64 / union as f64
}

/// Статистика совпадений одного документа (первый проход).
struct DocStats {
    /// Эффективная tf по каждому термину (с бустом заголовков).
    tf: Vec<f64>,
    /// Термин совпал с токеном имени файла.
    name_matched: Vec<bool>,
    /// По каждой строке — битовая маска совпавших терминов.
    line_masks: Vec<u64>,
}

/// Подсчёт совпадений терминов в документе (контент — если `use_content`,
/// иначе только имя файла).
fn doc_stats(
    file: &CachedFile,
    path: &Path,
    terms: &[String],
    term_chars: &[usize],
    mode: MatchMode,
    use_content: bool,
) -> DocStats {
    let n = terms.len();
    let mut tf = vec![0.0; n];
    let mut line_masks: Vec<u64> = vec![0; file.lines.len()];
    if use_content {
        for (line_idx, line) in file.lines.iter().enumerate() {
            let is_heading = file
                .headings
                .iter()
                .any(|(heading_idx, _)| *heading_idx == line_idx);
            for token in &line.tokens {
                for (t, term) in terms.iter().enumerate() {
                    if term_matches_token(mode, term, term_chars[t], token) {
                        line_masks[line_idx] |= 1u64 << t;
                        tf[t] += if is_heading { HEADING_BOOST } else { 1.0 };
                    }
                }
            }
        }
    }
    let name_tokens = path
        .file_stem()
        .and_then(|s| s.to_str())
        .map_or_else(Vec::new, tokenize);
    let mut name_matched = vec![false; n];
    for token in &name_tokens {
        for (t, term) in terms.iter().enumerate() {
            if term_matches_token(mode, term, term_chars[t], token) {
                name_matched[t] = true;
            }
        }
    }
    DocStats {
        tf,
        name_matched,
        line_masks,
    }
}

/// Скоринг корпуса: df/idf первым проходом, хиты вторым.
fn score_corpus(
    corpus: &CorpusCache,
    candidates: &[PathBuf],
    terms: &[String],
    options: &SearchOptions,
    mode: MatchMode,
) -> Vec<KbHit> {
    let term_chars: Vec<usize> = terms.iter().map(|t| t.chars().count()).collect();

    // Первый проход: tf, df, средняя длина документа.
    let mut stats: Vec<Option<DocStats>> = Vec::with_capacity(candidates.len());
    let mut df = vec![0usize; terms.len()];
    let mut total_tokens = 0usize;
    let mut n_docs = 0usize;
    for path in candidates {
        let Some(file) = corpus.files.get(path) else {
            stats.push(None);
            continue;
        };
        let use_content = !options.names_only && file.content.is_some();
        let stat = doc_stats(file, path, terms, &term_chars, mode, use_content);
        if use_content {
            n_docs += 1;
            total_tokens += file.doc_tokens;
            for (t, &count) in stat.tf.iter().enumerate() {
                if count > 0.0 {
                    df[t] += 1;
                }
            }
        } else {
            // names_only / большие файлы: df считается по имени.
            for (t, &matched) in stat.name_matched.iter().enumerate() {
                if matched {
                    df[t] += 1;
                }
            }
        }
        stats.push(Some(stat));
    }
    let any_match = stats
        .iter()
        .flatten()
        .any(|s| s.tf.iter().any(|&tf| tf > 0.0) || s.name_matched.iter().any(|&m| m));
    if !any_match {
        return Vec::new();
    }
    let avgdl = if n_docs > 0 {
        total_tokens as f64 / n_docs as f64
    } else {
        1.0
    };
    let idfs: Vec<f64> = df.iter().map(|&d| bm25_idf(n_docs.max(1), d)).collect();
    let mean_idf = idfs.iter().sum::<f64>() / idfs.len() as f64;
    let fuzzy_weight = if mode == MatchMode::Fuzzy {
        FUZZY_SCORE_WEIGHT
    } else {
        1.0
    };

    // Второй проход: скор документа и хиты по группам совпадений.
    let mut hits = Vec::new();
    for (path, stat) in candidates.iter().zip(stats) {
        let (Some(file), Some(stat)) = (corpus.files.get(path), stat) else {
            continue;
        };
        let mut score = 0.0;
        for (t, &tf) in stat.tf.iter().enumerate() {
            if tf > 0.0 {
                score += fuzzy_weight * idfs[t] * bm25_tf(tf, file.doc_tokens, avgdl);
            }
            if stat.name_matched[t] {
                score += fuzzy_weight * idfs[t] * NAME_BONUS;
            }
        }
        score += phrase_bonus(&stat.line_masks, terms.len(), mean_idf);
        hits.extend(score_file_hits(path, file, &stat, score, mode));
    }
    hits
}

/// BM25 idf: `ln(1 + (N - df + 0.5) / (df + 0.5))`.
fn bm25_idf(n_docs: usize, df: usize) -> f64 {
    (((n_docs - df) as f64 + 0.5) / (df as f64 + 0.5) + 1.0).ln()
}

/// BM25 насыщение tf с нормализацией длины документа.
fn bm25_tf(tf: f64, doc_tokens: usize, avgdl: f64) -> f64 {
    let norm = 1.0 - BM25_B + BM25_B * doc_tokens as f64 / avgdl.max(1.0);
    tf * (BM25_K1 + 1.0) / (tf + BM25_K1 * norm)
}

/// Фразовый бонус: все термины в одной строке — [`PHRASE_LINE_BONUS`],
/// в окне ±[`PHRASE_WINDOW_LINES`] строк — [`PHRASE_WINDOW_BONUS`]
/// (множители к среднему idf запроса).
fn phrase_bonus(line_masks: &[u64], n_terms: usize, mean_idf: f64) -> f64 {
    if n_terms < 2 {
        return 0.0;
    }
    let full = (1u64 << n_terms) - 1;
    if line_masks.contains(&full) {
        return PHRASE_LINE_BONUS * mean_idf;
    }
    for (idx, &mask) in line_masks.iter().enumerate() {
        if mask == 0 {
            continue;
        }
        let from = idx.saturating_sub(PHRASE_WINDOW_LINES);
        let to = (idx + PHRASE_WINDOW_LINES).min(line_masks.len().saturating_sub(1));
        let window_mask = line_masks[from..=to].iter().fold(0u64, |acc, &m| acc | m);
        if window_mask == full {
            return PHRASE_WINDOW_BONUS * mean_idf;
        }
    }
    0.0
}

/// Хиты одного файла: группы соседних совпадений или хит по имени.
fn score_file_hits(
    path: &Path,
    file: &CachedFile,
    stat: &DocStats,
    score: f64,
    mode: MatchMode,
) -> Vec<KbHit> {
    let fuzzy = mode == MatchMode::Fuzzy;
    let match_lines: Vec<usize> = stat
        .line_masks
        .iter()
        .enumerate()
        .filter(|&(_, &mask)| mask != 0)
        .map(|(idx, _)| idx)
        .collect();
    if match_lines.is_empty() {
        if stat.name_matched.iter().any(|&m| m) {
            let file_name = path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or_default();
            return vec![KbHit {
                path: path.to_path_buf(),
                line: 0,
                score,
                snippet: format!("совпадение по имени файла: {file_name}"),
                breadcrumb: String::new(),
                fuzzy,
                facets: file.frontmatter.clone(),
            }];
        }
        return Vec::new();
    }
    let Some(content) = &file.content else {
        return Vec::new();
    };

    // Соседние совпадения (зазор не больше двух контекстов) сливаются в один хит.
    let mut hits = Vec::new();
    let mut group_start = 0usize;
    for i in 1..=match_lines.len() {
        let same_group =
            i < match_lines.len() && match_lines[i] - match_lines[i - 1] <= 2 * CONTEXT_LINES + 1;
        if same_group {
            continue;
        }
        hits.push(make_hit(
            path,
            content,
            file,
            &stat.line_masks,
            &match_lines[group_start..i],
            score,
            fuzzy,
        ));
        group_start = i;
    }
    hits
}

/// Хит по группе соседних совпадений: лучшая строка + сниппет с контекстом.
fn make_hit(
    path: &Path,
    content: &str,
    file: &CachedFile,
    line_masks: &[u64],
    group: &[usize],
    score: f64,
    fuzzy: bool,
) -> KbHit {
    let best = best_line(file, line_masks, group);
    // Контекст сниппета не заходит в паспорт: `body_start` — граница тела.
    // Инвариант (совпадения бывают только в строках с непустой маской, а
    // паспортные строки маски не имеют) держит это и без клампа, но явная
    // граница страхует от будущих правок скоринга.
    let start = group
        .first()
        .copied()
        .unwrap_or(best)
        .saturating_sub(CONTEXT_LINES)
        .max(file.body_start);
    let end = group
        .last()
        .copied()
        .unwrap_or(best)
        .saturating_add(CONTEXT_LINES)
        .min(file.lines.len().saturating_sub(1));
    let group_set: HashSet<usize> = group.iter().copied().collect();

    let mut snippet = String::new();
    for (idx, line) in file.lines.iter().enumerate().take(end + 1).skip(start) {
        let marker = if group_set.contains(&idx) {
            ">>> "
        } else {
            "    "
        };
        let text = content.get(line.start..line.end).unwrap_or_default();
        // записи в String инфаллибильны
        let _ = writeln!(snippet, "{marker}{}", truncate_line(text.trim_end()));
    }
    KbHit {
        path: path.to_path_buf(),
        line: best + 1,
        score,
        snippet: snippet.trim_end().to_string(),
        breadcrumb: breadcrumb_at(file, best),
        fuzzy,
        facets: file.frontmatter.clone(),
    }
}

/// Лучшая строка группы: максимум различных терминов (фраза в одной строке
/// выигрывает естественно); при равенстве — заголовок, затем первая строка.
fn best_line(file: &CachedFile, line_masks: &[u64], group: &[usize]) -> usize {
    let mut best = group.first().copied().unwrap_or(0);
    let mut best_count = 0usize;
    let mut best_is_heading = false;
    for &idx in group {
        let count = line_masks.get(idx).copied().unwrap_or(0).count_ones() as usize;
        let is_heading = file
            .headings
            .iter()
            .any(|(heading_idx, _)| *heading_idx == idx);
        if count > best_count || (count == best_count && is_heading && !best_is_heading) {
            best = idx;
            best_count = count;
            best_is_heading = is_heading;
        }
    }
    best
}

/// Breadcrumb: ближайший заголовок markdown выше строки (включительно).
fn breadcrumb_at(file: &CachedFile, line_idx: usize) -> String {
    if !file.has_headings {
        return String::new();
    }
    file.headings
        .iter()
        .rev()
        .find(|(idx, _)| *idx <= line_idx)
        .map_or_else(String::new, |(_, title)| title.clone())
}

/// Разнообразие топа: не более [`MAX_HITS_PER_FILE`] хитов на файл;
/// при нехватке хитов до лимита остаток добирается из отложенных.
fn diversify(hits: Vec<KbHit>, limit: usize) -> Vec<KbHit> {
    let mut per_file: HashMap<PathBuf, usize> = HashMap::new();
    let mut out = Vec::with_capacity(limit);
    let mut deferred = Vec::new();
    for hit in hits {
        let count = per_file.entry(hit.path.clone()).or_insert(0);
        if *count < MAX_HITS_PER_FILE && out.len() < limit {
            *count += 1;
            out.push(hit);
        } else {
            deferred.push(hit);
        }
    }
    for hit in deferred {
        if out.len() >= limit {
            break;
        }
        out.push(hit);
    }
    out
}

/// LRU-выселение целых файлов при превышении бюджета [`CACHE_MAX_BYTES`].
fn evict_if_needed(corpus: &mut CorpusCache) {
    while corpus.total_bytes > CACHE_MAX_BYTES && !corpus.files.is_empty() {
        let Some(oldest) = corpus
            .files
            .iter()
            .min_by_key(|(_, file)| file.tick)
            .map(|(path, _)| path.clone())
        else {
            break;
        };
        if let Some(file) = corpus.files.remove(&oldest) {
            corpus.total_bytes = corpus.total_bytes.saturating_sub(file.accounted_bytes());
        }
    }
}

/// Усекает строку сниппета до [`MAX_SNIPPET_LINE`] символов.
fn truncate_line(line: &str) -> String {
    if line.chars().count() <= MAX_SNIPPET_LINE {
        line.to_string()
    } else {
        let cut: String = line.chars().take(MAX_SNIPPET_LINE).collect();
        format!("{cut}…")
    }
}

/// Аргументы инструмента `kb_search`.
#[derive(Debug, Deserialize)]
struct KbSearchArgs {
    /// Поисковый запрос (термины через пробел).
    query: String,
    /// Максимум хитов (по умолчанию 10, не больше 20).
    limit: Option<usize>,
    /// Подстрока или glob (`*`, `?`) по относительному пути файла.
    path_filter: Option<String>,
    /// Переопределение расширений конфига на этот вызов.
    extensions: Option<Vec<String>>,
    /// Режим «только имена» (содержимое не читается).
    names_only: Option<bool>,
    /// Фильтр по `type` паспорта: строка или массив (`digest` | `concept` |
    /// `playbook` | `synthesis` | `skill`).
    #[serde(default, deserialize_with = "string_or_seq")]
    filter_type: Vec<String>,
    /// Фильтр по `status`: `draft` (написано агентом) | `stable` (прошло ревью).
    #[serde(default, deserialize_with = "string_or_seq")]
    filter_status: Vec<String>,
    /// Фильтр по `concepts`: документ имеет хотя бы один из перечисленных тегов.
    #[serde(default, deserialize_with = "string_or_seq")]
    filter_concepts: Vec<String>,
}

/// Строка или массив строк → нормализованный список (lowercase/trim/дедуп).
/// Даёт агенту возможность передать `"filter_type": "digest"` и
/// `["digest", "playbook"]` одинаково.
fn string_or_seq<'de, D>(deserializer: D) -> std::result::Result<Vec<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    /// Одна строка либо массив строк (`` null `` — пустой список).
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum StringOrSeq {
        /// Одиночное значение.
        One(String),
        /// Список значений.
        Many(Vec<String>),
    }
    let raw = match Option::<StringOrSeq>::deserialize(deserializer)? {
        None => Vec::new(),
        Some(StringOrSeq::One(value)) => vec![value],
        Some(StringOrSeq::Many(values)) => values,
    };
    let mut out: Vec<String> = Vec::new();
    for value in raw {
        let norm = value.trim().to_lowercase();
        if !norm.is_empty() && !out.contains(&norm) {
            out.push(norm);
        }
    }
    Ok(out)
}

/// Строка-паспорт для вывода инструмента (`None` — печатать нечего).
fn facet_line(facets: &FrontmatterFacets) -> Option<String> {
    let mut parts: Vec<String> = Vec::new();
    if let Some(kind) = &facets.r#type {
        parts.push(format!("type={kind}"));
    }
    match facets.status.as_deref() {
        Some("stable") => parts.push("status=stable".into()),
        Some("draft") => parts.push("status=⚠ draft (не ревью)".into()),
        Some(other) => parts.push(format!("status={other}")),
        None => {}
    }
    if !facets.concepts.is_empty() {
        parts.push(format!("concepts={}", facets.concepts.join(",")));
    }
    if parts.is_empty() {
        None
    } else {
        Some(parts.join(" · "))
    }
}

/// Инструмент `kb_search`: поиск по локальной базе знаний из конфига.
struct KbSearchTool;

#[async_trait]
impl Tool for KbSearchTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "kb_search".into(),
            description: "Поиск по локальной базе знаний архитектора (каталоги knowledge.dirs из конфига). BM25-ранжирование со словоформами («идемпотентность» найдёт «идемпотентный») и фолбэком против опечаток. Возвращает хиты: путь, строка, раздел (breadcrumb), сниппет. Зовётся ПЕРЕД вебом. Примеры: query «transactional outbox»; сузить до подкаталога — path_filter «adr/» или glob «notes/*»; переопределить типы файлов — extensions [\"md\"]; найти файл по имени — names_only=true; сузить по паспорту — filter_type: \"digest\", filter_status: \"stable\" (только прошедшее ревью), filter_concepts: [\"dpo\"]; комбинируются логическим И.".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос: термины через пробел. Термины в одной строке документа дают фразовый бонус"},
                    "limit": {"type": "integer", "description": "Максимум хитов (по умолчанию 10, не больше 20; не более 2 хитов на файл, пока хватает других)", "default": 10},
                    "path_filter": {"type": "string", "description": "Фильтр по относительному пути: подстрока («adr/») или glob («notes/*», «adr-0??-*»). Используй, чтобы искать только в части базы"},
                    "extensions": {"type": "array", "items": {"type": "string"}, "description": "Расширения файлов на этот вызов вместо knowledge.extensions из конфига, напр. [\"md\"] или [\"txt\", \"rst\"]"},
                    "names_only": {"type": "boolean", "description": "true — быстрый поиск только по именам файлов, содержимое не читается. Для вопросов вида «где лежит файл про …»", "default": false},
                    "filter_type": {"type": ["string", "array"], "items": {"type": "string"}, "description": "Фильтр по типу паспорта frontmatter: digest | concept | playbook | synthesis | skill. Строка или массив (внутри — ИЛИ)"},
                    "filter_status": {"type": ["string", "array"], "items": {"type": "string"}, "description": "Фильтр по status паспорта: draft (написано агентом, не ревью) | stable (прошло ревью). Файл без паспорта под фильтр не попадает"},
                    "filter_concepts": {"type": ["string", "array"], "items": {"type": "string"}, "description": "Фильтр по concepts паспорта: документ имеет хотя бы один из тегов (напр. [\"dpo\", \"grpo\"])"}
                },
                "required": ["query"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: KbSearchArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("kb_search: невалидные аргументы: {e}")))?;
        let limit = args.limit.unwrap_or(10).min(20);
        let kb = &ctx.config.knowledge;
        let names_only = args.names_only.unwrap_or(false);
        let facets = FacetFilter {
            types: args.filter_type,
            statuses: args.filter_status,
            concepts: args.filter_concepts,
        };
        // names_only даёт запись без контента и без паспорта — фасетный фильтр
        // вернул бы 0 хитов всегда. Это гарантированная ошибка вызова.
        if !facets.is_empty() && names_only {
            return Ok(ToolOutput::err(
                "kb_search: фильтр по фасетам требует чтения содержимого — несовместим с names_only=true",
            ));
        }
        let options = SearchOptions {
            path_filter: args.path_filter,
            extensions: args.extensions,
            names_only,
            facets,
        };
        let hits = search_with(&kb.dirs, &kb.extensions, &args.query, limit, &options).await?;
        if hits.is_empty() {
            return Ok(ToolOutput::ok(format!(
                "По запросу «{}» в базе знаний ничего не найдено.",
                args.query
            )));
        }
        let mut buf = String::new();
        for hit in &hits {
            // записи в String инфаллибильны
            let _ = write!(
                buf,
                "── {}:{} (score {:.1})",
                hit.path.display(),
                hit.line,
                hit.score
            );
            if !hit.breadcrumb.is_empty() {
                let _ = write!(buf, " › {}", hit.breadcrumb);
            }
            if hit.fuzzy {
                let _ = write!(buf, " — нечёткое совпадение");
            }
            let _ = writeln!(buf);
            if let Some(fm) = &hit.facets {
                if let Some(line) = facet_line(fm) {
                    let _ = writeln!(buf, "   [{line}]");
                }
            }
            let _ = writeln!(buf, "{}", hit.snippet);
        }
        Ok(ToolOutput::ok(buf))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Корпус из трёх md-файлов во временном каталоге.
    fn corpus() -> tempfile::TempDir {
        let dir = tempfile::tempdir().expect("tempdir");
        let write = |name: &str, content: &str| {
            std::fs::write(dir.path().join(name), content).expect("write");
        };
        write(
            "adr-042-kafka.md",
            "# ADR-042\n\nБрокер сообщений.\nKafka выбран как шина событий.\nИтог: kafka в проде.\n",
        );
        write(
            "integration-notes.md",
            "# Интеграция\n\nKafka для обмена событиями.\nИтог: kafka в проде.\n",
        );
        write("readme.md", "# Общее\n\nНичего про брокеров.\n");
        dir
    }

    /// Пути, прочитанные kb с диска внутри `dir` (фильтр от параллельных тестов).
    fn reads_in(dir: &Path) -> Vec<PathBuf> {
        READ_LOG
            .lock()
            .expect("read log")
            .iter()
            .filter(|p| p.starts_with(dir))
            .cloned()
            .collect()
    }

    #[tokio::test]
    async fn ranks_filename_match_above_content_match() {
        let dir = corpus();
        let hits = search(&[dir.path().to_path_buf()], &["md".into()], "kafka", 10)
            .await
            .expect("search");
        assert_eq!(hits.len(), 2);
        let top = hits.first().expect("top hit");
        assert_eq!(
            top.path.file_name().and_then(|n| n.to_str()),
            Some("adr-042-kafka.md")
        );
        assert!(top.score > hits[1].score);
    }

    #[tokio::test]
    async fn snippet_marks_match_line_and_context() {
        let dir = corpus();
        let hits = search(&[dir.path().to_path_buf()], &["md".into()], "kafka", 10)
            .await
            .expect("search");
        let hit = hits
            .iter()
            .find(|h| h.path.ends_with("adr-042-kafka.md"))
            .expect("hit");
        assert_eq!(hit.line, 4);
        assert!(
            hit.snippet.contains(">>> Kafka выбран как шина событий."),
            "сниппет: {}",
            hit.snippet
        );
        assert!(
            hit.snippet.contains("    Брокер сообщений."),
            "контекст без маркера: {}",
            hit.snippet
        );
    }

    #[tokio::test]
    async fn respects_limit() {
        let dir = corpus();
        let hits = search(&[dir.path().to_path_buf()], &["md".into()], "kafka", 1)
            .await
            .expect("search");
        assert_eq!(hits.len(), 1);
    }

    #[tokio::test]
    async fn skips_hidden_dirs_and_foreign_extensions() {
        let dir = corpus();
        std::fs::write(dir.path().join("code.rs"), "fn kafka() {}").expect("write rs");
        let hidden = dir.path().join(".hidden");
        std::fs::create_dir(&hidden).expect("mkdir");
        std::fs::write(hidden.join("secret.md"), "kafka в скрытом каталоге").expect("write hidden");
        let hits = search(&[dir.path().to_path_buf()], &["md".into()], "kafka", 10)
            .await
            .expect("search");
        assert!(
            hits.iter()
                .all(|h| h.path.extension().and_then(|e| e.to_str()) == Some("md"))
        );
        assert!(
            hits.iter()
                .all(|h| !h.path.to_string_lossy().contains(".hidden"))
        );
    }

    #[tokio::test]
    async fn empty_query_returns_no_hits() {
        let dir = corpus();
        let hits = search(&[dir.path().to_path_buf()], &["md".into()], "   ", 10)
            .await
            .expect("search");
        assert!(hits.is_empty());
    }

    #[test]
    fn exposes_kb_search_tool() {
        let tools = tools();
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0].spec().name, "kb_search");
    }

    #[tokio::test]
    async fn cache_serves_repeat_queries_and_invalidates_on_change() {
        let dir = tempfile::tempdir().expect("tempdir");
        let file = dir.path().join("notes.md");
        std::fs::write(&file, "Первая редакция про альфаплатформу.\n").expect("write");
        let dirs = [dir.path().to_path_buf()];
        let exts = ["md".to_string()];

        let first = search(&dirs, &exts, "альфаплатформу", 10)
            .await
            .expect("first");
        assert_eq!(first.len(), 1);
        let reads_after_first = reads_in(dir.path()).len();
        assert_eq!(reads_after_first, 1, "первый поиск читает файл");

        // Повторный запрос: файлы не перечитываются, хиты идентичны.
        let second = search(&dirs, &exts, "альфаплатформу", 10)
            .await
            .expect("second");
        assert_eq!(reads_in(dir.path()).len(), reads_after_first);
        let key = |h: &KbHit| (h.path.clone(), h.line, h.snippet.clone());
        assert_eq!(
            first.iter().map(key).collect::<Vec<_>>(),
            second.iter().map(key).collect::<Vec<_>>()
        );

        // Изменение файла (другая длина) инвалидирует запись: новый контент виден.
        std::fs::write(
            &file,
            "Первая редакция про альфаплатформу.\nТеперь добавлена гаммафункция и детали.\n",
        )
        .expect("rewrite");
        let third = search(&dirs, &exts, "гаммафункция", 10)
            .await
            .expect("third");
        assert_eq!(third.len(), 1, "новый контент виден после изменения");
        assert!(
            reads_in(dir.path()).len() > reads_after_first,
            "изменённый файл перечитан"
        );
    }

    #[tokio::test]
    async fn idf_prefers_document_with_rare_term() {
        let dir = tempfile::tempdir().expect("tempdir");
        // Термин «альфатермин» — в 9 из 10 файлов, «бетатермин» — в одном.
        for i in 0..9 {
            std::fs::write(
                dir.path().join(format!("common-{i}.md")),
                "Заметка. Альфатермин встречается повсюду в корпусе.\n",
            )
            .expect("write");
        }
        std::fs::write(
            dir.path().join("rare.md"),
            "Заметка. Бетатермин уникален для этого документа.\n",
        )
        .expect("write");
        let hits = search(
            &[dir.path().to_path_buf()],
            &["md".into()],
            "альфатермин бетатермин",
            10,
        )
        .await
        .expect("search");
        let top = hits.first().expect("top hit");
        assert_eq!(
            top.path.file_name().and_then(|n| n.to_str()),
            Some("rare.md"),
            "документ с редким термином должен быть выше: {hits:?}"
        );
    }

    #[tokio::test]
    async fn wordforms_match_by_stem() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(
            dir.path().join("adj.md"),
            "Обработчик должен быть идемпотентный при ретраях.\n",
        )
        .expect("write");
        std::fs::write(
            dir.path().join("gen.md"),
            "Повторная доставка идемпотентного обработчика безопасна.\n",
        )
        .expect("write");
        std::fs::write(dir.path().join("other.md"), "Ничего про ретраи.\n").expect("write");
        let hits = search(
            &[dir.path().to_path_buf()],
            &["md".into()],
            "идемпотентность",
            10,
        )
        .await
        .expect("search");
        assert_eq!(hits.len(), 2, "обе словоформы найдены: {hits:?}");
        assert!(hits.iter().all(|h| !h.fuzzy), "точный корневой матч");
    }

    #[tokio::test]
    async fn phrase_in_one_line_beats_scattered_terms() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(
            dir.path().join("together.md"),
            "Шина transactional outbox гарантирует доставку события.\n",
        )
        .expect("write");
        std::fs::write(
            dir.path().join("apart.md"),
            "Паттерн transactional применяется широко.\n\n\n\n\nОтдельно обсудим outbox таблицу.\n",
        )
        .expect("write");
        let hits = search(
            &[dir.path().to_path_buf()],
            &["md".into()],
            "transactional outbox",
            10,
        )
        .await
        .expect("search");
        assert_eq!(hits.len(), 2);
        let score_of = |name: &str| {
            hits.iter()
                .find(|h| h.path.ends_with(name))
                .expect("hit")
                .score
        };
        assert!(
            score_of("together.md") > score_of("apart.md"),
            "фраза в одной строке выше: together={} apart={}",
            score_of("together.md"),
            score_of("apart.md")
        );
    }

    #[tokio::test]
    async fn markdown_hit_carries_breadcrumb() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(
            dir.path().join("spec.md"),
            "# Спецификация\n\n## Платежи\n\nПроверьте идемпотентность обработчика.\n\n## Отчёты\n\nПрочее.\n",
        )
        .expect("write");
        let hits = search(
            &[dir.path().to_path_buf()],
            &["md".into()],
            "идемпотентность",
            10,
        )
        .await
        .expect("search");
        let hit = hits.first().expect("hit");
        assert_eq!(hit.breadcrumb, "Платежи");
        assert_eq!(hit.line, 5);
    }

    #[tokio::test]
    async fn top_hits_are_diverse_per_file() {
        let dir = tempfile::tempdir().expect("tempdir");
        // Горячий файл: 5 разнесённых групп, в каждой термин дважды
        // (скор группы выше холодных файлов — без капа топ был бы весь его).
        let mut hot = String::new();
        for block in 0..5 {
            // записи в String инфаллибильны
            let _ = writeln!(
                hot,
                "Блок {block} про дельтаплатформу и ещё раз дельтаплатформу."
            );
            hot.push_str("Наполнитель без совпадений.\n".repeat(6).as_str());
        }
        std::fs::write(dir.path().join("hot.md"), hot).expect("write");
        for name in ["cold-a.md", "cold-b.md", "cold-c.md"] {
            std::fs::write(
                dir.path().join(name),
                "Заметка про дельтаплатформу в одном месте.\n",
            )
            .expect("write");
        }
        let dirs = [dir.path().to_path_buf()];
        let exts = ["md".to_string()];
        let hits = search(&dirs, &exts, "дельтаплатформу", 4)
            .await
            .expect("search");
        let hot_hits = hits.iter().filter(|h| h.path.ends_with("hot.md")).count();
        assert_eq!(
            hot_hits, MAX_HITS_PER_FILE,
            "горячий файл ограничен капом, хотя скорит выше: {hits:?}"
        );
        assert!(
            hits.iter().any(|h| !h.path.ends_with("hot.md")),
            "холодные файлы добирают топ: {hits:?}"
        );

        // Ослабление лимита на файл: матчит только горячий файл.
        let hits_relaxed = search_with(
            &dirs,
            &exts,
            "дельтаплатформу",
            4,
            &SearchOptions {
                path_filter: Some("hot.md".into()),
                extensions: None,
                names_only: false,
                facets: FacetFilter::default(),
            },
        )
        .await
        .expect("search relaxed");
        assert_eq!(
            hits_relaxed.len(),
            4,
            "остаток добирается из одного файла: {hits_relaxed:?}"
        );
    }

    #[tokio::test]
    async fn path_filter_extensions_and_names_only() {
        let dir = tempfile::tempdir().expect("tempdir");
        let adr = dir.path().join("adr");
        let notes = dir.path().join("notes");
        std::fs::create_dir(&adr).expect("mkdir");
        std::fs::create_dir(&notes).expect("mkdir");
        std::fs::write(adr.join("decision.md"), "Решение про сигмапаттерн.\n").expect("write");
        std::fs::write(notes.join("memo.md"), "Заметка про сигмапаттерн.\n").expect("write");
        std::fs::write(notes.join("memo.txt"), "Текст про сигмапаттерн.\n").expect("write");
        std::fs::write(adr.join("sigma-index.md"), "Пустая страница.\n").expect("write");
        let dirs = [dir.path().to_path_buf()];
        let exts = vec!["md".to_string(), "txt".to_string()];

        // path_filter: только подкаталог adr/.
        let hits = search_with(
            &dirs,
            &exts,
            "сигмапаттерн",
            10,
            &SearchOptions {
                path_filter: Some("adr/".into()),
                extensions: None,
                names_only: false,
                facets: FacetFilter::default(),
            },
        )
        .await
        .expect("path filter");
        assert_eq!(hits.len(), 1);
        assert!(hits[0].path.starts_with(&adr));

        // path_filter glob: только notes/*.
        let hits = search_with(
            &dirs,
            &exts,
            "сигмапаттерн",
            10,
            &SearchOptions {
                path_filter: Some("notes/*".into()),
                extensions: None,
                names_only: false,
                facets: FacetFilter::default(),
            },
        )
        .await
        .expect("path filter glob");
        assert_eq!(hits.len(), 2);
        assert!(hits.iter().all(|h| h.path.starts_with(&notes)));

        // extensions: переопределение на txt.
        let hits = search_with(
            &dirs,
            &exts,
            "сигмапаттерн",
            10,
            &SearchOptions {
                path_filter: None,
                extensions: Some(vec!["txt".into()]),
                names_only: false,
                facets: FacetFilter::default(),
            },
        )
        .await
        .expect("extensions");
        assert_eq!(hits.len(), 1);
        assert!(hits[0].path.ends_with("memo.txt"));

        // names_only: матч по имени без чтения содержимого.
        let hits = search_with(
            &dirs,
            &exts,
            "sigma",
            10,
            &SearchOptions {
                path_filter: None,
                extensions: None,
                names_only: true,
                facets: FacetFilter::default(),
            },
        )
        .await
        .expect("names only");
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].line, 0);
        assert!(hits[0].path.ends_with("sigma-index.md"));
    }

    #[tokio::test]
    async fn typo_falls_back_to_fuzzy_trigrams() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(
            dir.path().join("payments.md"),
            "Проверьте идемпотентность обработчика платежей.\n",
        )
        .expect("write");
        let hits = search(
            &[dir.path().to_path_buf()],
            &["md".into()],
            "идемпатентность",
            10,
        )
        .await
        .expect("search");
        assert_eq!(hits.len(), 1, "опечатка ловится фолбэком: {hits:?}");
        assert!(hits[0].fuzzy, "хит помечен как нечёткий");
    }

    #[tokio::test]
    async fn warm_cache_rereads_nothing_on_repeat_query() {
        let dir = tempfile::tempdir().expect("tempdir");
        for i in 0..200 {
            let mut text = String::new();
            for j in 0..200 {
                // записи в String инфаллибильны
                let _ = writeln!(text, "Строка {j} наполнитель бенчкорпуса файла {i}.");
            }
            std::fs::write(dir.path().join(format!("doc-{i:03}.md")), text).expect("write");
        }
        let dirs = [dir.path().to_path_buf()];
        let exts = ["md".to_string()];
        let first = search(&dirs, &exts, "бенчкорпуса", 10)
            .await
            .expect("warmup");
        assert_eq!(first.len(), 10);
        let reads_after_warmup = reads_in(dir.path()).len();

        let second = search(&dirs, &exts, "бенчкорпуса", 10)
            .await
            .expect("repeat");
        assert_eq!(
            reads_in(dir.path()).len(),
            reads_after_warmup,
            "горячий кэш: повторный запрос ничего не читает с диска"
        );
        let key = |h: &KbHit| (h.path.clone(), h.line);
        assert_eq!(
            first.iter().map(key).collect::<Vec<_>>(),
            second.iter().map(key).collect::<Vec<_>>()
        );
    }

    #[tokio::test]
    async fn tool_output_has_breadcrumb_and_fuzzy_marker() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(
            dir.path().join("spec.md"),
            "## Платежи\n\nПроверьте идемпотентность обработчика.\n",
        )
        .expect("write");
        let mut config = crate::config::Config::default();
        config.knowledge.dirs = vec![dir.path().to_path_buf()];
        config.knowledge.extensions = vec!["md".into()];
        let ctx = ToolContext::new(dir.path().to_path_buf(), Arc::new(config));
        let tool = KbSearchTool;

        // Точный хит: шапка с breadcrumb.
        let out = tool
            .call(json!({"query": "идемпотентность"}), &ctx)
            .await
            .expect("call");
        assert!(!out.is_error);
        assert!(
            out.content.contains("› Платежи"),
            "breadcrumb в шапке: {}",
            out.content
        );

        // Опечатка: пометка нечёткого совпадения.
        let out = tool
            .call(json!({"query": "идемпатентность"}), &ctx)
            .await
            .expect("call fuzzy");
        assert!(
            out.content.contains("нечёткое совпадение"),
            "пометка фолбэка: {}",
            out.content
        );
    }

    // ── Frontmatter-фасеты (P1) ──────────────────────────────────────────────

    /// Корпус из пар (имя, содержимое) во временном каталоге.
    fn write_corpus(files: &[(&str, &str)]) -> tempfile::TempDir {
        let dir = tempfile::tempdir().expect("tempdir");
        for (name, content) in files {
            std::fs::write(dir.path().join(name), content).expect("write");
        }
        dir
    }

    /// Фильтр по фасетам из строковых срезов (как нормализует инструмент).
    fn filter(types: &[&str], statuses: &[&str], concepts: &[&str]) -> FacetFilter {
        FacetFilter {
            types: types.iter().map(|s| (*s).to_string()).collect(),
            statuses: statuses.iter().map(|s| (*s).to_string()).collect(),
            concepts: concepts.iter().map(|s| (*s).to_string()).collect(),
        }
    }

    /// Поиск с фасетным фильтром в одном каталоге.
    async fn search_facets(dir: &Path, query: &str, facets: FacetFilter) -> Vec<KbHit> {
        let options = SearchOptions {
            facets,
            ..SearchOptions::default()
        };
        search_with(
            &[dir.to_path_buf()],
            &["md".to_string()],
            query,
            20,
            &options,
        )
        .await
        .expect("search")
    }

    /// Имена файлов хитов (для компактных проверок).
    fn file_names(hits: &[KbHit]) -> Vec<String> {
        let mut names: Vec<String> = hits
            .iter()
            .map(|h| {
                h.path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or_default()
                    .to_string()
            })
            .collect();
        names.sort();
        names
    }

    #[test]
    fn scan_frontmatter_parses_facets() {
        let text = "---\ntype: digest\nstatus: draft\nsource: arXiv:2501.1\nconcepts: [dpo, grpo]\ncreated: 2026-08-04\n---\n\nТело.\n";
        let (facets, body_start) = scan_frontmatter(text);
        let facets = facets.expect("паспорт разобран");
        assert_eq!(facets.r#type.as_deref(), Some("digest"));
        assert_eq!(facets.status.as_deref(), Some("draft"));
        assert_eq!(facets.source.as_deref(), Some("arXiv:2501.1"));
        assert_eq!(facets.concepts, vec!["dpo".to_string(), "grpo".to_string()]);
        assert_eq!(facets.created.as_deref(), Some("2026-08-04"));
        // Строка после закрывающего `---` (индекс 6 в zero-based).
        assert_eq!(body_start, 7);
    }

    #[test]
    fn scan_frontmatter_tolerates_colon_in_description() {
        // `description: Текст: с двоеточием` — невалидный YAML: включается
        // построчный фолбэк, нужные фасеты всё равно извлечены.
        let text =
            "---\ntype: digest\ndescription: Текст: с двоеточием\nstatus: stable\n---\n\nТело.\n";
        let (facets, body_start) = scan_frontmatter(text);
        let facets = facets.expect("паспорт разобран фолбэком");
        assert_eq!(facets.r#type.as_deref(), Some("digest"));
        assert_eq!(facets.status.as_deref(), Some("stable"));
        assert_eq!(body_start, 5);
    }

    #[test]
    fn scan_frontmatter_accepts_leading_html_comment() {
        let text = "<!-- filename: x.md -->\n---\ntype: concept\n---\n\nТело.\n";
        let (facets, body_start) = scan_frontmatter(text);
        let facets = facets.expect("паспорт найден после комментария");
        assert_eq!(facets.r#type.as_deref(), Some("concept"));
        assert_eq!(body_start, 4);
    }

    #[test]
    fn scan_frontmatter_absent_and_broken_are_none() {
        let (facets, body_start) = scan_frontmatter("Просто текст без паспорта.\n");
        assert!(facets.is_none());
        assert_eq!(body_start, 0);
        // Незакрытый `---` паспортом не считается.
        let (facets, body_start) = scan_frontmatter("---\ntype: digest\n\nТело.\n");
        assert!(facets.is_none());
        assert_eq!(body_start, 0);
    }

    #[tokio::test]
    async fn frontmatter_lines_excluded_from_body_scoring() {
        let dir = write_corpus(&[(
            "card.md",
            "---\ntype: digest\nstatus: draft\n---\n\nТело карточки про шину событий.\n",
        )]);
        // Слова паспорта не индексируются как тело: ни `digest`, ни `draft`
        // не дают контентного хита, хотя присутствуют в файле.
        assert!(
            search_facets(dir.path(), "digest", FacetFilter::default())
                .await
                .is_empty()
        );
        assert!(
            search_facets(dir.path(), "draft", FacetFilter::default())
                .await
                .is_empty()
        );
    }

    /// Три файла: digest/draft, concept/stable и файл без паспорта.
    fn facet_corpus() -> tempfile::TempDir {
        write_corpus(&[
            (
                "a.md",
                "---\ntype: digest\nstatus: draft\n---\n\nkafka как шина событий.\n",
            ),
            (
                "b.md",
                "---\ntype: concept\nstatus: stable\n---\n\nkafka в продакшене.\n",
            ),
            ("c.md", "kafka без паспорта.\n"),
        ])
    }

    #[tokio::test]
    async fn filter_type_and_status_narrows_corpus() {
        let dir = facet_corpus();
        let all = search_facets(dir.path(), "kafka", FacetFilter::default()).await;
        assert_eq!(file_names(&all), vec!["a.md", "b.md", "c.md"]);

        let by_type = search_facets(dir.path(), "kafka", filter(&["digest"], &[], &[])).await;
        assert_eq!(file_names(&by_type), vec!["a.md"]);

        let by_status = search_facets(dir.path(), "kafka", filter(&[], &["stable"], &[])).await;
        assert_eq!(file_names(&by_status), vec!["b.md"]);
    }

    #[tokio::test]
    async fn filter_within_field_is_or_between_fields_is_and() {
        let dir = facet_corpus();
        let either = search_facets(
            dir.path(),
            "kafka",
            filter(&["digest", "concept"], &[], &[]),
        )
        .await;
        assert_eq!(file_names(&either), vec!["a.md", "b.md"]);

        let only_stable = search_facets(
            dir.path(),
            "kafka",
            filter(&["digest", "concept"], &["stable"], &[]),
        )
        .await;
        assert_eq!(file_names(&only_stable), vec!["b.md"]);
    }

    #[tokio::test]
    async fn filter_concepts_matches_any_tag() {
        let dir = write_corpus(&[("card.md", "---\nconcepts: [a, b]\n---\n\nkafka как шина.\n")]);
        assert_eq!(
            file_names(&search_facets(dir.path(), "kafka", filter(&[], &[], &["b"])).await),
            vec!["card.md"]
        );
        assert!(
            search_facets(dir.path(), "kafka", filter(&[], &[], &["zzz"]))
                .await
                .is_empty()
        );
    }

    #[tokio::test]
    async fn filter_excludes_docs_without_frontmatter() {
        let dir = facet_corpus();
        // c.md (без паспорта) не проходит ни один непустой фильтр.
        let hits = search_facets(dir.path(), "kafka", filter(&[], &["draft"], &[])).await;
        assert_eq!(file_names(&hits), vec!["a.md"]);
        assert!(!file_names(&hits).contains(&"c.md".to_string()));
    }

    #[tokio::test]
    async fn filter_is_case_insensitive() {
        let dir = write_corpus(&[(
            "card.md",
            "---\nType: Digest\nStatus: Draft\n---\n\nkafka как шина.\n",
        )]);
        let hits = search_facets(dir.path(), "kafka", filter(&["digest"], &["draft"], &[])).await;
        assert_eq!(file_names(&hits), vec!["card.md"]);
        let facets = hits
            .first()
            .and_then(|h| h.facets.as_ref())
            .expect("паспорт");
        assert_eq!(facets.r#type.as_deref(), Some("digest"));
        assert_eq!(facets.status.as_deref(), Some("draft"));
    }

    #[tokio::test]
    async fn filter_conflicts_with_names_only() {
        let dir = facet_corpus();
        let mut config = crate::config::Config::default();
        config.knowledge.dirs = vec![dir.path().to_path_buf()];
        config.knowledge.extensions = vec!["md".into()];
        let ctx = ToolContext::new(dir.path().to_path_buf(), Arc::new(config));
        let out = KbSearchTool
            .call(
                json!({"query": "kafka", "names_only": true, "filter_type": "digest"}),
                &ctx,
            )
            .await
            .expect("call");
        assert!(out.is_error);
        assert!(
            out.content.contains("names_only"),
            "сообщение об ошибке: {}",
            out.content
        );
    }

    #[tokio::test]
    async fn filter_corpus_affects_idf() {
        // a.md (digest) — длинный наполнитель; b.md (concept) — короткий.
        // Фильтр сужает корпус ДО BM25-статистик, поэтому скор одного и того
        // же b.md в суженом и полном корпусе различается (защита от «фильтр
        // после скоринга»).
        let mut long = String::from("---\ntype: digest\nstatus: draft\n---\n\nkafka шина.\n");
        for i in 0..60 {
            // Запись в String не может завершиться ошибкой — игнор безопасен.
            let _ = writeln!(
                long,
                "Наполнитель строки номер {i} с уникальным словом филлер{i}."
            );
        }
        let dir = write_corpus(&[
            ("a.md", long.as_str()),
            (
                "b.md",
                "---\ntype: concept\nstatus: stable\n---\n\nkafka в продакшене.\n",
            ),
        ]);

        let full = search_facets(dir.path(), "kafka", FacetFilter::default()).await;
        let filtered = search_facets(dir.path(), "kafka", filter(&["concept"], &[], &[])).await;
        assert_eq!(file_names(&filtered), vec!["b.md"]);
        assert_eq!(file_names(&full), vec!["a.md", "b.md"]);

        let score_of = |hits: &[KbHit], name: &str| {
            hits.iter()
                .find(|h| h.path.ends_with(name))
                .map(|h| h.score)
                .expect("хит b.md")
        };
        let full_score = score_of(&full, "b.md");
        let filtered_score = score_of(&filtered, "b.md");
        assert!(
            (full_score - filtered_score).abs() > 1e-9,
            "корпус фильтра должен менять BM25-статистики: full={full_score}, filtered={filtered_score}"
        );
    }

    #[tokio::test]
    async fn tool_output_prints_draft_facet_line() {
        let dir = write_corpus(&[(
            "card.md",
            "---\ntype: digest\nstatus: draft\nconcepts: [grpo, reward]\n---\n\nkafka как шина.\n",
        )]);
        let mut config = crate::config::Config::default();
        config.knowledge.dirs = vec![dir.path().to_path_buf()];
        config.knowledge.extensions = vec!["md".into()];
        let ctx = ToolContext::new(dir.path().to_path_buf(), Arc::new(config));
        let out = KbSearchTool
            .call(json!({"query": "kafka"}), &ctx)
            .await
            .expect("call");
        assert!(!out.is_error);
        assert!(
            out.content.contains("type=digest"),
            "паспорт в выдаче: {}",
            out.content
        );
        assert!(
            out.content.contains("⚠ draft (не ревью)"),
            "маркер черновика: {}",
            out.content
        );
        assert!(
            out.content.contains("concepts=grpo,reward"),
            "концепты в выдаче: {}",
            out.content
        );
    }

    #[test]
    fn kb_hit_serde_defaults_without_facets() {
        let json = r#"{"path":"/tmp/x.md","line":1,"score":0.5,"snippet":"s"}"#;
        let hit: KbHit = serde_json::from_str(json).expect("десериализация без facets");
        assert!(hit.facets.is_none());
        assert!(!hit.fuzzy);
        assert!(hit.breadcrumb.is_empty());

        let hit = KbHit {
            path: PathBuf::from("/tmp/x.md"),
            line: 1,
            score: 0.5,
            snippet: "s".into(),
            breadcrumb: String::new(),
            fuzzy: false,
            facets: Some(FrontmatterFacets {
                r#type: Some("digest".into()),
                status: Some("draft".into()),
                concepts: vec!["dpo".into()],
                ..FrontmatterFacets::default()
            }),
        };
        let text = serde_json::to_string(&hit).expect("сериализация");
        let back: KbHit = serde_json::from_str(&text).expect("round-trip");
        assert_eq!(back.facets, hit.facets);
    }

    #[tokio::test]
    async fn search_without_facets_unchanged() {
        // Пустой фильтр = прежнее поведение: `search` и `search_with` с дефолтными
        // опциями дают один и тот же топ.
        let dir = corpus();
        let plain = search(&[dir.path().to_path_buf()], &["md".into()], "kafka", 10)
            .await
            .expect("search");
        let with_opts = search_facets(dir.path(), "kafka", FacetFilter::default()).await;
        assert_eq!(file_names(&plain), file_names(&with_opts));
        assert_eq!(plain.len(), with_opts.len());
    }
}
