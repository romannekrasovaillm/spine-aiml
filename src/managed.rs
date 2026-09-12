//! Managed-блоки и якорные диффы (P1: протокол перезаписи генерируемого фрагмента).
//!
//! КОНТРАКТ (грунт — `library/distillate/1_методология/`):
//! - managed-блок ограничен парой маркеров `<!-- agent:managed:begin … -->` …
//!   `<!-- agent:managed:end id=… -->`, занимающих строку целиком; внутри —
//!   тело, адресуемое по `id` со сверкой `hash` (обнаружение ручной правки —
//!   merge-конфликт, а не молчаливая перезапись);
//! - якорные секционные операции адресуются путём заголовков markdown
//!   (память), а не номерами строк: сдвиг строк не влияет на адрес;
//! - три инварианта: **идемпотентность** (повторный прогон пакета —
//!   побайтово тот же файл), **атомарность** (tmp + rename; при ошибке файл
//!   не тронут), **provenance** (`id` + `ver` + `source`).
//!
//! Модуль — библиотечный слой (не [`crate::tool::Tool`]): его потребители —
//! `distill`/`agentsmd`/cron, а не LLM в первом лице.
//!
//! ```no_run
//! use arch_harness::managed::{Patch, BlockOp, HashGuard, Sha256, Anchor, apply};
//! let text = "# Правила\n";
//! let patch = Patch {
//!     target: "AGENTS.md".into(),
//!     base_hash: HashGuard::Expect(Sha256::of_text(text)),
//!     block_ops: vec![BlockOp::Insert {
//!         anchor: Anchor::Eof, id: "rules".into(),
//!         content: "## Правило\n- одно".into(), source: None,
//!     }],
//!     section_ops: Vec::new(),
//! };
//! let (new_text, report) = apply(text, &patch).expect("apply");
//! assert!(report.changed && new_text.contains("agent:managed:begin"));
//! ```

use std::ops::Range;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::archunit::sha256_hex;
use crate::error::{HarnessError, Result};

/// Префикс пары маркеров (полная строка, после `trim`).
const MARKER_BEGIN: &str = "agent:managed:begin";
/// Имя парного закрывающего маркера.
const MARKER_END: &str = "agent:managed:end";
/// Счётчик уникальности tmp-имён (вместе с pid отсекает параллельные процессы).
static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Ошибка managed-блоков и якорных диффов (P1).
///
/// Обёрнут в [`HarnessError::Managed`]; здесь — структурный источник правды
/// с программно различаемыми вариантами.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum ManagedError {
    /// Файл изменился с момента сборки диффа (fail-fast).
    #[error(
        "base_hash mismatch: ожидалось {expected}, в файле {actual} — файл не изменён; перечитайте файл и пересоберите дифф"
    )]
    BaseHashMismatch {
        /// Ожидаемый хеш (из пакета).
        expected: Sha256,
        /// Фактический хеш файла.
        actual: Sha256,
    },

    /// Блок правили вручную: заявленный hash ≠ hash тела.
    #[error(
        "hash mismatch в блоке '{id}' (ver={ver}): ожидалось {expected}, фактически {actual} — блок модифицирован вручную; guard=Force затрёт правку"
    )]
    HashMismatch {
        /// Идентификатор блока.
        id: String,
        /// Заявленная версия блока.
        ver: u32,
        /// Ожидаемый хеш (из `guard`).
        expected: Sha256,
        /// Фактический хеш тела.
        actual: Sha256,
    },

    /// Блока нет.
    #[error("блок '{id}' не найден в {path}")]
    BlockNotFound {
        /// Идентификатор блока.
        id: String,
        /// Файл, в котором искали.
        path: PathBuf,
    },

    /// Блок с таким id уже есть.
    #[error("блок '{id}' уже существует (ver={ver}) — для обновления используйте Replace")]
    DuplicateId {
        /// Идентификатор блока.
        id: String,
        /// Версия существующего блока.
        ver: u32,
    },

    /// Незакрытый begin-маркер.
    #[error("строка {line}: begin-маркер '{id}' без парного end")]
    UnclosedBlock {
        /// Идентификатор блока.
        id: String,
        /// 1-based номер строки begin-маркера.
        line: usize,
    },

    /// end без begin.
    #[error("строка {line}: end-маркер '{id}' без парного begin")]
    OrphanEnd {
        /// Идентификатор из end-маркера.
        id: String,
        /// 1-based номер строки.
        line: usize,
    },

    /// begin внутри блока.
    #[error("строка {line}: begin-маркер '{id}' внутри блока '{outer}' — вложенность запрещена")]
    NestedBlock {
        /// Идентификатор вложенного begin-маркера.
        id: String,
        /// Идентификатор объемлющего блока.
        outer: String,
        /// 1-based номер строки.
        line: usize,
    },

    /// id в end ≠ id в begin.
    #[error("строка {line}: end-маркер '{got}' не совпадает с begin '{want}'")]
    MarkerIdMismatch {
        /// Ожидаемый id (из begin).
        want: String,
        /// Полученный id (из end).
        got: String,
        /// 1-based номер строки.
        line: usize,
    },

    /// Синтаксис маркера.
    #[error("строка {line}: невалидный маркер — {reason}")]
    BadMarker {
        /// 1-based номер строки (0 — не привязано к строке).
        line: usize,
        /// Причина отказа.
        reason: String,
    },

    /// `hash=` не sha256.
    #[error("невалидный hash '{raw}': ожидается sha256:<64 hex>")]
    BadHash {
        /// Сырое значение атрибута.
        raw: String,
    },

    /// Неизвестный атрибут маркера.
    #[error("строка {line}: неизвестный атрибут '{attr}' в маркере")]
    UnknownAttr {
        /// Имя неизвестного атрибута.
        attr: String,
        /// 1-based номер строки.
        line: usize,
    },

    /// Путь заголовков не найден.
    #[error("якорь {anchor} не найден в {path}")]
    HeadingNotFound {
        /// Путь заголовков (человекочитаемо).
        anchor: String,
        /// Целевой файл.
        path: PathBuf,
    },

    /// Путь заголовков неоднозначен (дубликат заголовка).
    #[error("якорь {anchor} неоднозначен: {count} совпадений")]
    AmbiguousHeading {
        /// Путь заголовков (человекочитаемо).
        anchor: String,
        /// Число совпадений.
        count: usize,
    },

    /// Плохой уровень заголовка.
    #[error("уровень заголовка {level} вне диапазона 1..=6")]
    BadLevel {
        /// Запрошенный уровень.
        level: u8,
    },

    /// Некорректный id блока.
    #[error("невалидный id '{id}': нужен kebab-case [a-z0-9][a-z0-9-]{{0,63}}")]
    BadId {
        /// Сырое значение id.
        id: String,
    },
}

/// Конверсия в общую ошибку — на этой стороне границы: `HarnessError`
/// остаётся листом и не знает типов `managed` (`leaf_modules_stay_leaf`).
impl From<ManagedError> for HarnessError {
    fn from(e: ManagedError) -> Self {
        HarnessError::Managed(e.to_string())
    }
}

/// SHA-256 в hex (lowercase).
///
/// Обёртка над [`crate::archunit::sha256_hex`]: даёт типу право быть ключом
/// сверки и печататься как `sha256:<hex>`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Sha256(String);

impl Sha256 {
    /// Хеш сырых байт.
    #[must_use]
    pub fn of_bytes(bytes: &[u8]) -> Self {
        Self(sha256_hex(bytes))
    }

    /// Хеш текста после канонизации (`\r\n`→`\n`, срез хвостовых `\n`).
    #[must_use]
    pub fn of_text(text: &str) -> Self {
        Self::of_bytes(canonical_body(text).as_bytes())
    }

    /// Разбор `sha256:<hex>` либо голого `<hex>` (64 символа, lowercase).
    ///
    /// # Errors
    /// [`ManagedError::BadHash`] — не 64 hex-символа.
    pub fn parse(raw: &str) -> Result<Self> {
        Self::parse_typed(raw).map_err(Into::into)
    }

    /// Типизированный разбор (для внутренних путей: линтер, маркеры).
    fn parse_typed(raw: &str) -> std::result::Result<Self, ManagedError> {
        let hex = raw.strip_prefix("sha256:").unwrap_or(raw);
        let valid = hex.len() == 64
            && hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
        if !valid {
            return Err(ManagedError::BadHash {
                raw: raw.to_string(),
            });
        }
        Ok(Self(hex.to_string()))
    }

    /// Голый hex без префикса.
    #[must_use]
    pub fn as_hex(&self) -> &str {
        &self.0
    }

    /// Значение в виде `sha256:<hex>` (для маркера и отчётов).
    #[must_use]
    pub fn as_marker(&self) -> String {
        format!("sha256:{}", self.0)
    }
}

impl std::fmt::Display for Sha256 {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("sha256:")?;
        f.write_str(&self.0)
    }
}

/// Managed-блок с байтовыми границами в исходном тексте.
#[derive(Debug, Clone)]
pub struct Block {
    /// Идентификатор блока (kebab-case).
    pub id: String,
    /// Версия содержимого (монотонна, >= 1).
    pub ver: u32,
    /// Хеш ТЕЛА блока, заявленный в маркере (см. [`canonical_body`]).
    pub hash: Sha256,
    /// Источник (provenance), напр. `article-lib` или `arXiv:2402.03300`.
    pub source: Option<String>,
    /// Канонизированное тело (без маркеров).
    pub body: String,
    /// 1-based номер строки `begin`-маркера.
    pub begin_line: usize,
    /// 1-based номер строки `end`-маркера.
    pub end_line: usize,
    /// Байтовый диапазон `begin..end` включительно с хвостовым `\n` end-строки.
    pub span: Range<usize>,
}

/// Разрешение на перезапись.
///
/// `Force` конструируется только явно — молчаливой перезаписи чужой правки
/// в API нет.
#[derive(Debug, Clone)]
pub enum HashGuard {
    /// Сверка: текущее значение обязано совпасть, иначе [`ManagedError::HashMismatch`].
    Expect(Sha256),
    /// Явный форс (человек/`--force`): пишем поверх, факт фиксируется в отчёте.
    Force,
}

/// Куда вставлять новый блок.
#[derive(Debug, Clone)]
pub enum Anchor {
    /// После блока с указанным id.
    AfterBlock(String),
    /// Перед блоком с указанным id.
    BeforeBlock(String),
    /// В конец файла (отдельной секцией через пустую строку).
    Eof,
    /// В начало файла.
    Bof,
}

/// Операция над managed-блоком (адресация по `id`).
#[derive(Debug, Clone)]
pub enum BlockOp {
    /// Заменить тело блока; `ver` → `ver+1`.
    /// `upsert = true` — нет блока с таким id: создать на `anchor`.
    Replace {
        /// Идентификатор блока.
        id: String,
        /// Разрешение на перезапись.
        guard: HashGuard,
        /// Новое тело блока.
        content: String,
        /// Источник (provenance).
        source: Option<String>,
        /// Создавать блок при отсутствии.
        upsert: bool,
        /// Якорь создания (при `upsert`).
        anchor: Anchor,
    },
    /// Вставить НОВЫЙ блок. Идемпотентна: id уже есть с тем же телом → no-op.
    Insert {
        /// Куда вставлять.
        anchor: Anchor,
        /// Идентификатор блока.
        id: String,
        /// Тело блока.
        content: String,
        /// Источник (provenance).
        source: Option<String>,
    },
    /// Удалить блок вместе с маркерами. Отсутствие → [`ManagedError::BlockNotFound`].
    Remove {
        /// Идентификатор блока.
        id: String,
        /// Разрешение на удаление.
        guard: HashGuard,
    },
}

/// Куда вставлять секцию относительно якорного заголовка.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Position {
    /// Перед якорным заголовком.
    Before,
    /// После всей секции якоря.
    After,
    /// Первой внутри секции якоря.
    ChildFirst,
    /// Последней внутри секции якоря.
    ChildLast,
}

/// Операция над секцией (адресация путём заголовков — «память»).
#[derive(Debug, Clone)]
pub enum SectionOp {
    /// Заменить тело секции, сохранив её заголовок.
    Replace {
        /// Путь заголовков.
        anchor: Vec<String>,
        /// Новое тело секции.
        content: String,
    },
    /// Вставить секцию. Если дочерний заголовок с тем же текстом уже есть —
    /// деградирует в `Replace` (идемпотентность ретрая).
    Insert {
        /// Путь заголовков якоря.
        anchor: Vec<String>,
        /// Позиция вставки.
        position: Position,
        /// Текст нового заголовка (без `#`).
        heading: String,
        /// Уровень заголовка (1..=6).
        level: u8,
        /// Тело новой секции.
        content: String,
    },
    /// Дописать в конец тела секции.
    Append {
        /// Путь заголовков.
        anchor: Vec<String>,
        /// Дописываемый фрагмент.
        content: String,
    },
}

/// Пакет изменений одного файла: база + операции.
#[derive(Debug, Clone)]
pub struct Patch {
    /// Целевой файл.
    pub target: PathBuf,
    /// Сверка состояния ВСЕГО файла до применения (fail-fast на устаревшем диффе).
    pub base_hash: HashGuard,
    /// Операции над managed-блоками.
    pub block_ops: Vec<BlockOp>,
    /// Якорные операции над секциями.
    pub section_ops: Vec<SectionOp>,
}

/// Итог применения.
#[derive(Debug, Clone)]
pub struct ApplyReport {
    /// Путь файла.
    pub path: PathBuf,
    /// Хеш файла до применения.
    pub old_hash: Sha256,
    /// Хеш после применения (или равен `old_hash` при `changed == false`).
    pub new_hash: Sha256,
    /// false → идемпотентный no-op; файл побайтово не изменился.
    pub changed: bool,
    /// Выполнена ли атомарная запись (false при no-op).
    pub written: bool,
    /// Затронутые id блоков (в порядке применения).
    pub blocks_touched: Vec<String>,
    /// Блоки, снятые/перезаписанные принудительно (`HashGuard::Force`).
    pub forced: Vec<String>,
}

/// Находка линтера managed-блоков.
#[derive(Debug, Clone)]
pub struct ManagedIssue {
    /// Путь файла (или `<text>` для [`lint_text`]).
    pub file: PathBuf,
    /// 1-based строка (0 — файл целиком).
    pub line: usize,
    /// Правило: `unclosed_block` | `orphan_end` | `duplicate_id` |
    /// `bad_marker` | `unknown_attr` | `hash_mismatch` | `no_blocks`.
    pub rule: String,
    /// Человекочитаемое сообщение (русский).
    pub message: String,
    /// `error` | `warn`.
    pub severity: String,
}

/// Канонизировать тело (`\r\n`→`\n`, срез хвостовых `\n`). Идемпотентна.
#[must_use]
pub fn canonical_body(body: &str) -> String {
    let lf = if body.contains('\r') {
        body.replace("\r\n", "\n")
    } else {
        body.to_string()
    };
    lf.trim_end_matches('\n').to_string()
}

/// Строка исходного текста с байтовыми границами (`end` включает `\n`).
struct Line<'a> {
    text: &'a str,
    start: usize,
    end: usize,
}

/// Разбить текст на строки, сохраняя байтовые смещения.
fn lines_with_offsets(text: &str) -> Vec<Line<'_>> {
    let mut out = Vec::new();
    let mut start = 0usize;
    for chunk in text.split_inclusive('\n') {
        let end = start + chunk.len();
        out.push(Line {
            text: chunk,
            start,
            end,
        });
        start = end;
    }
    out
}

/// Разобранные атрибуты begin-маркера.
struct BeginAttrs {
    id: String,
    ver: u32,
    hash: Sha256,
    source: Option<String>,
}

/// Разобранные атрибуты end-маркера.
struct EndAttrs {
    id: String,
}

/// Вид распознанного маркера.
enum MarkerKind {
    Begin(BeginAttrs),
    End(EndAttrs),
}

/// Проверить id на kebab-case `[a-z0-9][a-z0-9-]{0,63}`.
fn valid_id(id: &str) -> bool {
    if id.is_empty() || id.len() > 64 {
        return false;
    }
    let first_ok = id
        .chars()
        .next()
        .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit());
    first_ok
        && id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

/// Снять кавычки с `ts=`-значения (допускаются `"…"` и `'…'`).
fn unquote(raw: &str) -> String {
    let t = raw.trim();
    let bytes = t.as_bytes();
    if bytes.len() >= 2 {
        let (a, b) = (bytes[0], bytes[bytes.len() - 1]);
        if (a == b'"' && b == b'"') || (a == b'\'' && b == b'\'') {
            return t[1..t.len() - 1].to_string();
        }
    }
    t.to_string()
}

/// Распознать строку как маркер (строка целиком, после `trim`).
fn recognize_marker(
    trimmed: &str,
    line_no: usize,
) -> std::result::Result<Option<MarkerKind>, ManagedError> {
    let Some(inner) = trimmed
        .strip_prefix("<!--")
        .and_then(|s| s.strip_suffix("-->"))
    else {
        return Ok(None);
    };
    let inner = inner.trim();
    let mut tokens = inner.split_whitespace();
    let Some(head) = tokens.next() else {
        return Ok(None);
    };
    let is_begin = head == MARKER_BEGIN;
    let is_end = head == MARKER_END;
    if !is_begin && !is_end {
        return Ok(None);
    }

    let mut id: Option<String> = None;
    let mut ver: Option<u32> = None;
    let mut hash: Option<Sha256> = None;
    let mut source: Option<String> = None;

    for tok in tokens {
        let Some((key, value)) = tok.split_once('=') else {
            return Err(ManagedError::BadMarker {
                line: line_no,
                reason: format!("атрибут '{tok}' без '='"),
            }
            .into());
        };
        match key {
            "id" => {
                if !valid_id(value) {
                    return Err(ManagedError::BadId {
                        id: value.to_string(),
                    }
                    .into());
                }
                id = Some(value.to_string());
            }
            "ver" if is_begin => {
                let parsed = value.parse::<u32>().ok().filter(|v| *v >= 1);
                match parsed {
                    Some(v) => ver = Some(v),
                    None => {
                        return Err(ManagedError::BadMarker {
                            line: line_no,
                            reason: format!("ver='{value}' — ожидается uint >= 1"),
                        }
                        .into());
                    }
                }
            }
            "hash" if is_begin => hash = Some(Sha256::parse_typed(value)?),
            "source" if is_begin => source = Some(value.to_string()),
            "ts" if is_begin => {
                let _ = unquote(value);
            }
            other => {
                return Err(ManagedError::UnknownAttr {
                    attr: other.to_string(),
                    line: line_no,
                }
                .into());
            }
        }
    }

    if is_end {
        let Some(id) = id else {
            return Err(ManagedError::BadMarker {
                line: line_no,
                reason: "у end-маркера нет атрибута id".to_string(),
            }
            .into());
        };
        return Ok(Some(MarkerKind::End(EndAttrs { id })));
    }

    let Some(id) = id else {
        return Err(ManagedError::BadMarker {
            line: line_no,
            reason: "у begin-маркера нет атрибута id".to_string(),
        }
        .into());
    };
    let Some(ver) = ver else {
        return Err(ManagedError::BadMarker {
            line: line_no,
            reason: "у begin-маркера нет обязательного атрибута ver=".to_string(),
        }
        .into());
    };
    let Some(hash) = hash else {
        return Err(ManagedError::BadMarker {
            line: line_no,
            reason: "у begin-маркера нет обязательного hash=".to_string(),
        }
        .into());
    };
    Ok(Some(MarkerKind::Begin(BeginAttrs {
        id,
        ver,
        hash,
        source,
    })))
}

/// Открытый (ещё не закрытый) begin-маркер в процессе разбора.
struct PendingBegin {
    id: String,
    ver: u32,
    hash: Sha256,
    source: Option<String>,
    begin_line: usize,
    begin_start: usize,
    body_start: usize,
}

/// Разобрать все managed-блоки текста. Порядок — по позиции в файле.
///
/// # Errors
/// [`ManagedError::UnclosedBlock`] / [`ManagedError::OrphanEnd`] /
/// [`ManagedError::NestedBlock`] / [`ManagedError::MarkerIdMismatch`] /
/// [`ManagedError::DuplicateId`] / [`ManagedError::BadMarker`] /
/// [`ManagedError::BadHash`] / [`ManagedError::UnknownAttr`].
pub fn parse_blocks(text: &str) -> Result<Vec<Block>> {
    parse_blocks_impl(text).map_err(Into::into)
}

/// Ядро разбора на типизированной ошибке (листовая граница — обёртка выше).
fn parse_blocks_impl(text: &str) -> std::result::Result<Vec<Block>, ManagedError> {
    let lines = lines_with_offsets(text);
    let mut blocks: Vec<Block> = Vec::new();
    let mut pending: Option<PendingBegin> = None;

    for (idx, line) in lines.iter().enumerate() {
        let line_no = idx + 1;
        let trimmed = line.text.trim();
        let Some(kind) = recognize_marker(trimmed, line_no)? else {
            continue;
        };
        match kind {
            MarkerKind::Begin(attrs) => {
                if let Some(open) = &pending {
                    return Err(ManagedError::NestedBlock {
                        id: attrs.id,
                        outer: open.id.clone(),
                        line: line_no,
                    }
                    .into());
                }
                pending = Some(PendingBegin {
                    id: attrs.id,
                    ver: attrs.ver,
                    hash: attrs.hash,
                    source: attrs.source,
                    begin_line: line_no,
                    begin_start: line.start,
                    body_start: line.end,
                });
            }
            MarkerKind::End(attrs) => {
                let Some(open) = pending.take() else {
                    return Err(ManagedError::OrphanEnd {
                        id: attrs.id,
                        line: line_no,
                    }
                    .into());
                };
                if open.id != attrs.id {
                    return Err(ManagedError::MarkerIdMismatch {
                        want: open.id,
                        got: attrs.id,
                        line: line_no,
                    }
                    .into());
                }
                if blocks.iter().any(|b| b.id == open.id) {
                    return Err(ManagedError::DuplicateId {
                        id: open.id,
                        ver: open.ver,
                    }
                    .into());
                }
                let body = canonical_body(&text[open.body_start..line.start]);
                blocks.push(Block {
                    id: open.id,
                    ver: open.ver,
                    hash: open.hash,
                    source: open.source,
                    body,
                    begin_line: open.begin_line,
                    end_line: line_no,
                    span: open.begin_start..line.end,
                });
            }
        }
    }

    if let Some(open) = pending {
        return Err(ManagedError::UnclosedBlock {
            id: open.id,
            line: open.begin_line,
        }
        .into());
    }
    Ok(blocks)
}

/// Найти блок по id.
///
/// # Errors
/// [`ManagedError::BlockNotFound`].
pub fn find_block<'a>(blocks: &'a [Block], id: &str) -> Result<&'a Block> {
    blocks.iter().find(|b| b.id == id).ok_or_else(|| {
        ManagedError::BlockNotFound {
            id: id.to_string(),
            path: PathBuf::from("<text>"),
        }
        .into()
    })
}

/// Отрендерить строку begin-маркера.
fn render_begin_line(id: &str, ver: u32, hash: &Sha256, source: Option<&str>) -> String {
    let mut line = format!(
        "<!-- {MARKER_BEGIN} id={id} ver={ver} hash={}",
        hash.as_marker()
    );
    if let Some(src) = source {
        line.push_str(" source=");
        line.push_str(src);
    }
    line.push_str(" -->");
    line
}

/// Отрендерить блок целиком (маркеры + тело). `hash` считается вызывающим.
#[must_use]
pub fn render_block(id: &str, ver: u32, hash: &Sha256, source: Option<&str>, body: &str) -> String {
    let canonical = canonical_body(body);
    let begin = render_begin_line(id, ver, hash, source);
    let end = format!("<!-- {MARKER_END} id={id} -->");
    if canonical.is_empty() {
        format!("{begin}\n{end}\n")
    } else {
        format!("{begin}\n{canonical}\n{end}\n")
    }
}

/// Заголовок markdown с байтовыми границами.
#[derive(Debug, Clone)]
struct Heading {
    /// Уровень `#` (1..=6).
    level: usize,
    /// Текст заголовка (без решёток, обрезан).
    title: String,
    /// Байтовое смещение начала строки заголовка.
    line_start: usize,
    /// Байтовое смещение конца строки заголовка (включая `\n`).
    heading_end: usize,
}

/// Разобрать заголовки markdown в порядке следования.
fn parse_headings(text: &str) -> Vec<Heading> {
    let mut out = Vec::new();
    for line in lines_with_offsets(text) {
        let trimmed = line.text.trim_end_matches(['\n', '\r']);
        let hashes = trimmed.chars().take_while(|c| *c == '#').count();
        if !(1..=6).contains(&hashes) {
            continue;
        }
        let rest = &trimmed[hashes..];
        if !rest.is_empty() && !rest.starts_with(' ') {
            continue;
        }
        let title = rest.trim().to_string();
        if title.is_empty() {
            continue;
        }
        out.push(Heading {
            level: hashes,
            title,
            line_start: line.start,
            heading_end: line.end,
        });
    }
    out
}

/// Конец тела секции: начало следующего заголовка уровня <= уровня секции.
fn section_end(text: &str, headings: &[Heading], idx: usize) -> usize {
    let level = headings[idx].level;
    for h in &headings[idx + 1..] {
        if h.level <= level {
            return h.line_start;
        }
    }
    text.len()
}

/// Разрешить путь заголовков в индекс заголовка.
///
/// # Errors
/// [`ManagedError::HeadingNotFound`] / [`ManagedError::AmbiguousHeading`].
fn resolve_section(
    text: &str,
    headings: &[Heading],
    anchor: &[String],
    path: &Path,
) -> std::result::Result<usize, ManagedError> {
    let display = anchor.join(" > ");
    if anchor.is_empty() {
        return Err(ManagedError::HeadingNotFound {
            anchor: display,
            path: path.to_path_buf(),
        }
        .into());
    }
    let mut start = 0usize;
    let mut end = text.len();
    let mut min_level = 1usize;
    let mut current: Option<usize> = None;

    for comp in anchor {
        let matches: Vec<usize> = headings
            .iter()
            .enumerate()
            .filter(|(_, h)| {
                h.line_start >= start
                    && h.line_start < end
                    && h.level >= min_level
                    && h.title == *comp
            })
            .map(|(i, _)| i)
            .collect();
        if matches.is_empty() {
            return Err(ManagedError::HeadingNotFound {
                anchor: display,
                path: path.to_path_buf(),
            }
            .into());
        }
        if matches.len() > 1 {
            return Err(ManagedError::AmbiguousHeading {
                anchor: display,
                count: matches.len(),
            }
            .into());
        }
        let i = matches[0];
        start = headings[i].heading_end;
        end = section_end(text, headings, i);
        min_level = headings[i].level + 1;
        current = Some(i);
    }

    current.ok_or_else(|| {
        ManagedError::HeadingNotFound {
            anchor: display,
            path: path.to_path_buf(),
        }
        .into()
    })
}

/// Разделить строку на содержимое без хвостовых переводов строк и сам хвост.
fn split_trailing_newlines(s: &str) -> (&str, &str) {
    let trimmed = s.trim_end_matches(['\n', '\r']);
    (&s[..trimmed.len()], &s[trimmed.len()..])
}

/// Вставить фрагмент в позицию, обеспечив пустую строку-разделитель.
fn splice_chunk(text: &str, pos: usize, chunk: &str) -> String {
    let pos = pos.min(text.len());
    let mut out = String::with_capacity(text.len() + chunk.len() + 4);
    out.push_str(&text[..pos]);
    if !out.is_empty() && !out.ends_with("\n\n") {
        if out.ends_with('\n') {
            out.push('\n');
        } else {
            out.push_str("\n\n");
        }
    }
    out.push_str(chunk);
    let rest = &text[pos..];
    if !rest.is_empty() && !rest.starts_with('\n') {
        out.push('\n');
    }
    out.push_str(rest);
    out
}

/// Схлопнуть пустые строки на стыке удаления (>=3 `\n` → 2).
fn collapse_at(text: &mut String, pos: usize) {
    let pos = pos.min(text.len());
    let start = text[..pos].rfind(|c| c != '\n').map_or(0, |i| i + 1);
    let end = text[pos..]
        .find(|c| c != '\n')
        .map_or(text.len(), |i| pos + i);
    if text[start..end].matches('\n').count() > 2 {
        text.replace_range(start..end, "\n\n");
    }
}

/// Найти байтовый диапазон блока по id (для якорей вставки).
fn find_block_span(
    text: &str,
    id: &str,
    path: &Path,
) -> std::result::Result<Range<usize>, ManagedError> {
    let blocks = parse_blocks_impl(text)?;
    blocks
        .iter()
        .find(|b| b.id == id)
        .map(|b| b.span.clone())
        .ok_or_else(|| {
            ManagedError::BlockNotFound {
                id: id.to_string(),
                path: path.to_path_buf(),
            }
            .into()
        })
}

/// Сверить тело блока с guard; при `Force` отметить id в отчёте.
fn check_guard(
    id: &str,
    ver: u32,
    body: &str,
    guard: &HashGuard,
    forced: &mut Vec<String>,
) -> std::result::Result<(), ManagedError> {
    let actual = Sha256::of_text(body);
    match guard {
        HashGuard::Expect(expected) => {
            if expected != &actual {
                return Err(ManagedError::HashMismatch {
                    id: id.to_string(),
                    ver,
                    expected: expected.clone(),
                    actual,
                }
                .into());
            }
        }
        HashGuard::Force => forced.push(id.to_string()),
    }
    Ok(())
}

/// Вставить отрендеренный блок по якорю.
fn insert_at_anchor(
    cur: &mut String,
    anchor: &Anchor,
    rendered: &str,
    path: &Path,
) -> std::result::Result<(), ManagedError> {
    let pos = match anchor {
        Anchor::Eof => cur.len(),
        Anchor::Bof => 0,
        Anchor::AfterBlock(id) => find_block_span(cur, id, path)?.end,
        Anchor::BeforeBlock(id) => find_block_span(cur, id, path)?.start,
    };
    *cur = splice_chunk(cur, pos, rendered);
    Ok(())
}

/// Применить одну операцию над блоком.
fn apply_block_op(
    cur: &mut String,
    op: &BlockOp,
    path: &Path,
    touched: &mut Vec<String>,
    forced: &mut Vec<String>,
) -> std::result::Result<(), ManagedError> {
    match op {
        BlockOp::Replace {
            id,
            guard,
            content,
            source,
            upsert,
            anchor,
        } => {
            let blocks = parse_blocks_impl(cur)?;
            let found = blocks
                .iter()
                .find(|b| &b.id == id)
                .map(|b| (b.span.clone(), b.ver, b.body.clone(), b.source.clone()));
            if let Some((span, ver, body, old_source)) = found {
                check_guard(id, ver, &body, guard, forced)?;
                let new_body = canonical_body(content);
                if new_body == body && source.as_deref() == old_source.as_deref() {
                    touched.push(id.clone());
                    return Ok(());
                }
                let new_ver = ver.saturating_add(1);
                let hash = Sha256::of_text(&new_body);
                let rendered = render_block(id, new_ver, &hash, source.as_deref(), &new_body);
                cur.replace_range(span, &rendered);
                touched.push(id.clone());
            } else {
                if !*upsert {
                    return Err(ManagedError::BlockNotFound {
                        id: id.clone(),
                        path: path.to_path_buf(),
                    }
                    .into());
                }
                let new_body = canonical_body(content);
                let hash = Sha256::of_text(&new_body);
                let rendered = render_block(id, 1, &hash, source.as_deref(), &new_body);
                insert_at_anchor(cur, anchor, &rendered, path)?;
                touched.push(id.clone());
            }
        }
        BlockOp::Insert {
            anchor,
            id,
            content,
            source,
        } => {
            let blocks = parse_blocks_impl(cur)?;
            if let Some(block) = blocks.iter().find(|b| &b.id == id) {
                if canonical_body(content) == block.body
                    && source.as_deref() == block.source.as_deref()
                {
                    return Ok(());
                }
                return Err(ManagedError::DuplicateId {
                    id: id.clone(),
                    ver: block.ver,
                }
                .into());
            }
            let new_body = canonical_body(content);
            let hash = Sha256::of_text(&new_body);
            let rendered = render_block(id, 1, &hash, source.as_deref(), &new_body);
            insert_at_anchor(cur, anchor, &rendered, path)?;
            touched.push(id.clone());
        }
        BlockOp::Remove { id, guard } => {
            let blocks = parse_blocks_impl(cur)?;
            let found = blocks
                .iter()
                .find(|b| &b.id == id)
                .map(|b| (b.span.clone(), b.ver, b.body.clone()));
            let Some((span, ver, body)) = found else {
                return Err(ManagedError::BlockNotFound {
                    id: id.clone(),
                    path: path.to_path_buf(),
                }
                .into());
            };
            check_guard(id, ver, &body, guard, forced)?;
            let at = span.start;
            cur.replace_range(span, "");
            collapse_at(cur, at);
            touched.push(id.clone());
        }
    }
    Ok(())
}

/// Заменить тело секции, сохранив заголовок.
fn replace_section_body(cur: &mut String, headings: &[Heading], idx: usize, content: &str) {
    let start = headings[idx].heading_end;
    let end = section_end(cur, headings, idx);
    let body = cur[start..end].to_string();
    let canon_content = canonical_body(content);
    if canonical_body(&body) == canon_content {
        return;
    }
    let (_, trailing) = split_trailing_newlines(&body);
    let tail = if trailing.is_empty() { "\n" } else { trailing };
    let new_body = format!("{canon_content}{tail}");
    cur.replace_range(start..end, &new_body);
}

/// Применить одну операцию над секцией.
fn apply_section_op(
    cur: &mut String,
    op: &SectionOp,
    path: &Path,
    _touched: &mut Vec<String>,
) -> std::result::Result<(), ManagedError> {
    match op {
        SectionOp::Replace { anchor, content } => {
            let headings = parse_headings(cur);
            let idx = resolve_section(cur, &headings, anchor, path)?;
            replace_section_body(cur, &headings, idx, content);
        }
        SectionOp::Insert {
            anchor,
            position,
            heading,
            level,
            content,
        } => {
            if !(1..=6).contains(level) {
                return Err(ManagedError::BadLevel { level: *level }.into());
            }
            let headings = parse_headings(cur);
            let idx = resolve_section(cur, &headings, anchor, path)?;
            let anchor_heading = headings[idx].clone();
            let sec_start = anchor_heading.heading_end;
            let sec_end = section_end(cur, &headings, idx);
            let existing = headings
                .iter()
                .enumerate()
                .find(|(_, h)| {
                    h.line_start >= sec_start && h.line_start < sec_end && h.title == *heading
                })
                .map(|(i, _)| i);
            if let Some(child) = existing {
                replace_section_body(cur, &headings, child, content);
                return Ok(());
            }
            let new_section = format!(
                "{} {}\n{}\n",
                "#".repeat(usize::from(*level)),
                heading,
                canonical_body(content)
            );
            let pos = match position {
                Position::Before => anchor_heading.line_start,
                Position::After | Position::ChildLast => sec_end,
                Position::ChildFirst => sec_start,
            };
            *cur = splice_chunk(cur, pos, &new_section);
        }
        SectionOp::Append { anchor, content } => {
            let headings = parse_headings(cur);
            let idx = resolve_section(cur, &headings, anchor, path)?;
            let start = headings[idx].heading_end;
            let end = section_end(cur, &headings, idx);
            let body = cur[start..end].to_string();
            let canon_content = canonical_body(content);
            let (content_part, trailing) = split_trailing_newlines(&body);
            if canonical_body(content_part).ends_with(&canon_content) {
                return Ok(());
            }
            let tail = if trailing.is_empty() { "\n" } else { trailing };
            let new_body = if content_part.is_empty() {
                format!("{canon_content}{tail}")
            } else {
                format!("{content_part}\n{canon_content}{tail}")
            };
            cur.replace_range(start..end, &new_body);
        }
    }
    Ok(())
}

/// Применить пакет к тексту в памяти. НИЧЕГО не пишет на диск.
///
/// Проверки `base_hash`/`guard` выполняются здесь — до любой записи.
///
/// # Errors
/// [`ManagedError::BaseHashMismatch`] / [`ManagedError::HashMismatch`] /
/// [`ManagedError::BlockNotFound`] / [`ManagedError::DuplicateId`] /
/// [`ManagedError::BadId`] / [`ManagedError::AmbiguousHeading`] /
/// [`ManagedError::HeadingNotFound`].
pub fn apply(text: &str, patch: &Patch) -> Result<(String, ApplyReport)> {
    apply_impl(text, patch).map_err(Into::into)
}

/// Ядро применения на типизированной ошибке (граница — обёртка выше).
fn apply_impl(
    text: &str,
    patch: &Patch,
) -> std::result::Result<(String, ApplyReport), ManagedError> {
    let path = patch.target.clone();
    let old_hash = Sha256::of_text(text);

    // Шаг 1: сверка базы (fail-fast) + структурная валидность.
    if let HashGuard::Expect(expected) = &patch.base_hash {
        if expected != &old_hash {
            return Err(ManagedError::BaseHashMismatch {
                expected: expected.clone(),
                actual: old_hash,
            }
            .into());
        }
    }
    parse_blocks_impl(text)?;

    // Шаг 2: применение операций к строке в памяти.
    let mut cur = text.to_string();
    let mut touched: Vec<String> = Vec::new();
    let mut forced: Vec<String> = Vec::new();
    for op in &patch.block_ops {
        apply_block_op(&mut cur, op, &path, &mut touched, &mut forced)?;
    }
    for op in &patch.section_ops {
        apply_section_op(&mut cur, op, &path, &mut touched)?;
    }

    // Шаг 3: верификация результата.
    let blocks = parse_blocks_impl(&cur)?;
    for id in &touched {
        if let Some(b) = blocks.iter().find(|b| &b.id == id) {
            let actual = Sha256::of_text(&b.body);
            if actual != b.hash {
                return Err(ManagedError::BadMarker {
                    line: b.begin_line,
                    reason: format!("после применения хеш блока '{id}' не совпал с телом"),
                }
                .into());
            }
        }
    }

    let changed = cur != text;
    let new_hash = if changed {
        Sha256::of_text(&cur)
    } else {
        old_hash.clone()
    };
    let report = ApplyReport {
        path,
        old_hash,
        new_hash,
        changed,
        written: false,
        blocks_touched: touched,
        forced,
    };
    Ok((cur, report))
}

/// Атомарная запись: tmp в ТОМ ЖЕ каталоге → flush → `sync_all` → rename.
///
/// Известное ограничение: права существующего файла при `rename` наследуются
/// от tmp (создаётся харнессом); при необходимости вызывающий выставляет их
/// отдельно.
///
/// # Errors
/// Ошибка создания каталога/записи/синхронизации/rename.
pub fn atomic_write(path: &Path, contents: &str) -> Result<()> {
    let dir = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(dir).map_err(|e| HarnessError::io(dir, e))?;

    let file_name = path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("managed");
    let counter = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let tmp = dir.join(format!(".{file_name}.{}.{counter}.tmp", std::process::id()));

    let write_result = (|| -> std::io::Result<()> {
        use std::io::Write as _;
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(contents.as_bytes())?;
        f.flush()?;
        f.sync_all()?;
        Ok(())
    })();

    if let Err(e) = write_result {
        let _ = std::fs::remove_file(&tmp);
        return Err(HarnessError::io(&tmp, e));
    }
    if let Err(e) = std::fs::rename(&tmp, path) {
        let _ = std::fs::remove_file(&tmp);
        return Err(HarnessError::io(path, e));
    }
    Ok(())
}

/// Ожидает ли пакет существующей базы (для отсутствующего файла).
fn requires_existing_base(patch: &Patch) -> bool {
    !patch.section_ops.is_empty()
        || patch.block_ops.iter().any(|op| match op {
            BlockOp::Remove { .. } => true,
            BlockOp::Replace { upsert, .. } => !*upsert,
            BlockOp::Insert { .. } => false,
        })
}

/// Прочитать файл и применить пакет, с атомарной записью результата.
///
/// При любой ошибке файл НЕ изменён.
///
/// # Errors
/// См. [`apply`] + ошибки чтения/записи/rename.
pub fn apply_file(patch: &Patch) -> Result<ApplyReport> {
    let text = match std::fs::read_to_string(&patch.target) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            if requires_existing_base(patch) {
                let expected = match &patch.base_hash {
                    HashGuard::Expect(h) => h.clone(),
                    HashGuard::Force => Sha256::of_text(""),
                };
                return Err(ManagedError::BaseHashMismatch {
                    expected,
                    actual: Sha256::of_text(""),
                }
                .into());
            }
            String::new()
        }
        Err(e) => return Err(HarnessError::io(&patch.target, e)),
    };
    let (new_text, mut report) = apply_impl(&text, patch)?;
    if report.changed {
        atomic_write(&patch.target, &new_text)?;
        report.written = true;
    }
    Ok(report)
}

/// Асинхронная обёртка (для `Tool::call`): [`apply_file`] в `spawn_blocking`.
///
/// # Errors
/// См. [`apply_file`]; ошибка join-а — [`HarnessError::Managed`].
pub async fn apply_file_async(patch: Patch) -> Result<ApplyReport> {
    tokio::task::spawn_blocking(move || apply_file(&patch))
        .await
        .map_err(|e| ManagedError::BadMarker {
            line: 0,
            reason: format!("join apply_file: {e}"),
        })?
}

/// Преобразовать ошибку разбора в находку линтера.
fn issue_from_error(err: &ManagedError, file: PathBuf) -> ManagedIssue {
    let (rule, line, message) = match err {
        ManagedError::UnclosedBlock { id, line } => (
            "unclosed_block",
            *line,
            format!("begin-маркер '{id}' без парного end"),
        ),
        ManagedError::OrphanEnd { id, line } => (
            "orphan_end",
            *line,
            format!("end-маркер '{id}' без парного begin"),
        ),
        ManagedError::DuplicateId { id, ver } => (
            "duplicate_id",
            0,
            format!("блок '{id}' объявлен дважды (ver={ver})"),
        ),
        ManagedError::UnknownAttr { attr, line } => (
            "unknown_attr",
            *line,
            format!("неизвестный атрибут '{attr}' в маркере"),
        ),
        other => ("bad_marker", 0, other.to_string()),
    };
    ManagedIssue {
        file,
        line,
        rule: rule.to_string(),
        message,
        severity: "error".to_string(),
    }
}

/// Линтер: структурные проблемы блоков (без I/O над файлом).
#[must_use]
pub fn lint_text(text: &str) -> Vec<ManagedIssue> {
    lint_with_file(text, PathBuf::from("<text>"))
}

/// Общий линтер с подстановкой пути файла.
fn lint_with_file(text: &str, file: PathBuf) -> Vec<ManagedIssue> {
    match parse_blocks_impl(text) {
        Err(e) => vec![issue_from_error(&e, file)],
        Ok(blocks) => {
            if blocks.is_empty() {
                return vec![ManagedIssue {
                    file,
                    line: 0,
                    rule: "no_blocks".to_string(),
                    message: "managed-блоков не найдено".to_string(),
                    severity: "warn".to_string(),
                }];
            }
            let mut issues = Vec::new();
            for b in &blocks {
                let actual = Sha256::of_text(&b.body);
                if actual != b.hash {
                    issues.push(ManagedIssue {
                        file: file.clone(),
                        line: b.begin_line,
                        rule: "hash_mismatch".to_string(),
                        message: format!(
                            "блок '{}' (ver={}) модифицирован вручную после генерации: заявлен {}, тело {}",
                            b.id, b.ver, b.hash, actual
                        ),
                        severity: "error".to_string(),
                    });
                }
            }
            issues
        }
    }
}

/// Линтер файла: структура + сверка заявленного хеша с телом.
///
/// # Errors
/// Файл не читается.
pub fn lint_file(path: &Path) -> Result<Vec<ManagedIssue>> {
    let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    Ok(lint_with_file(&text, path.to_path_buf()))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Хеш канонизированного тела для сборки маркеров в тестах.
    fn sha(body: &str) -> Sha256 {
        Sha256::of_text(body)
    }

    /// Строка begin-маркера с реальным хешем тела.
    fn begin(id: &str, ver: u32, body: &str) -> String {
        format!(
            "<!-- {MARKER_BEGIN} id={id} ver={ver} hash={} -->",
            Sha256::of_text(body).as_marker()
        )
    }

    /// Строка end-маркера.
    fn end(id: &str) -> String {
        format!("<!-- {MARKER_END} id={id} -->")
    }

    /// Пакет с базой `Expect(hash(text))`.
    fn patch(text: &str, block_ops: Vec<BlockOp>, section_ops: Vec<SectionOp>) -> Patch {
        Patch {
            target: PathBuf::from("<text>"),
            base_hash: HashGuard::Expect(Sha256::of_text(text)),
            block_ops,
            section_ops,
        }
    }

    /// Извлечь [`ManagedError`] из типизированного результата или упасть.
    fn managed_err<T>(res: std::result::Result<T, ManagedError>) -> ManagedError {
        res.err().expect("ожидалась ManagedError")
    }

    /// Пере-рендерить все блоки текста канонически (для проверки неподвижной точки).
    fn rerender(text: &str) -> String {
        let blocks = parse_blocks(text).expect("parse blocks");
        let mut out = String::with_capacity(text.len());
        let mut cursor = 0usize;
        for b in &blocks {
            out.push_str(&text[cursor..b.span.start]);
            out.push_str(&render_block(
                &b.id,
                b.ver,
                &b.hash,
                b.source.as_deref(),
                &b.body,
            ));
            cursor = b.span.end;
        }
        out.push_str(&text[cursor..]);
        out
    }

    // --- Чистые операции (без I/O) ---

    #[test]
    fn parse_roundtrip_is_fixed_point() {
        let b1 = render_block(
            "alpha",
            1,
            &sha("## Alpha\nтело alpha"),
            Some("article-lib"),
            "## Alpha\nтело alpha",
        );
        let b2 = render_block("beta", 2, &sha("тело beta"), None, "тело beta");
        let text =
            format!("# Правила проекта\n\nЧеловеческая зона.\n\n{b1}\nМежду блоками.\n\n{b2}");
        let blocks = parse_blocks(&text).expect("parse");
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0].id, "alpha");
        assert_eq!(blocks[1].id, "beta");
        // render(parse(t)) == t: блоки уже в каноническом виде.
        assert_eq!(rerender(&text), text);
    }

    #[test]
    fn canonical_body_idempotent_and_crlf() {
        assert_eq!(canonical_body("a\r\nb\r\n"), "a\nb");
        assert_eq!(canonical_body("x\n\n\n"), "x");
        assert_eq!(canonical_body(""), "");
        let once = canonical_body("a\r\nb\n\n");
        assert_eq!(canonical_body(&once), once);
    }

    #[test]
    fn parse_rejects_unclosed_orphan_nested_duplicate() {
        let unclosed = format!("{}\nbody\n", begin("a", 1, "body"));
        assert!(matches!(
            managed_err(parse_blocks_impl(&unclosed)),
            ManagedError::UnclosedBlock { .. }
        ));

        let orphan = format!("{}\n", end("a"));
        assert!(matches!(
            managed_err(parse_blocks_impl(&orphan)),
            ManagedError::OrphanEnd { .. }
        ));

        let nested = format!(
            "{}\nouter\n{}\ninner\n{}\n{}\n",
            begin("a", 1, "outer"),
            begin("b", 1, "inner"),
            end("b"),
            end("a")
        );
        assert!(matches!(
            managed_err(parse_blocks_impl(&nested)),
            ManagedError::NestedBlock { .. }
        ));

        let one = render_block("a", 1, &sha("t"), None, "t");
        let duplicate = format!("{one}{one}");
        assert!(matches!(
            managed_err(parse_blocks_impl(&duplicate)),
            ManagedError::DuplicateId { .. }
        ));

        let mismatch = format!("{}\nx\n{}\n", begin("a", 1, "x"), end("b"));
        assert!(matches!(
            managed_err(parse_blocks_impl(&mismatch)),
            ManagedError::MarkerIdMismatch { .. }
        ));
    }

    #[test]
    fn parse_ignores_markers_inside_text_lines() {
        // Маркер обязан занимать строку целиком: упоминания в прозе (как в
        // спеке протокола) не парсятся.
        let inline = "Вот `<!-- agent:managed:begin id=x ver=1 hash=sha256:00 -->` в строке.\n";
        assert!(parse_blocks(inline).expect("parse").is_empty());

        let prefixed = format!("текст до {} и после\n", begin("x", 1, "t"));
        assert!(parse_blocks(&prefixed).expect("parse").is_empty());

        let suffixed = format!("{} хвост\n", end("x"));
        assert!(parse_blocks(&suffixed).expect("parse").is_empty());
    }

    #[test]
    fn replace_block_bumps_ver_and_hash() {
        let body = "старое тело";
        let text = format!("{}\n{body}\n{}\n", begin("a", 2, body), end("a"));
        let p = patch(
            &text,
            vec![BlockOp::Replace {
                id: "a".into(),
                guard: HashGuard::Expect(sha(body)),
                content: "новое тело".into(),
                source: None,
                upsert: false,
                anchor: Anchor::Eof,
            }],
            Vec::new(),
        );
        let (out, rep) = apply(&text, &p).expect("apply");
        assert!(rep.changed);
        let blocks = parse_blocks(&out).expect("parse");
        let b = find_block(&blocks, "a").expect("block");
        assert_eq!(b.ver, 3, "ver монотонно растёт: 2 -> 3");
        assert_eq!(b.hash, sha("новое тело"));
        assert_eq!(b.body, "новое тело");
    }

    #[test]
    fn replace_with_expected_hash_mismatch_fails_and_writes_nothing() {
        let body = "тело";
        let original = format!("{}\n{body}\n{}\n", begin("a", 1, body), end("a"));
        let p = patch(
            &original,
            vec![BlockOp::Replace {
                id: "a".into(),
                guard: HashGuard::Expect(sha("другой хеш")),
                content: "новое".into(),
                source: None,
                upsert: false,
                anchor: Anchor::Eof,
            }],
            Vec::new(),
        );
        assert!(matches!(
            managed_err(apply_impl(&original, &p)),
            ManagedError::HashMismatch { .. }
        ));
        // apply чистый: исходный текст не мог измениться.
        assert_eq!(
            original,
            format!("{}\n{body}\n{}\n", begin("a", 1, body), end("a"))
        );
    }

    #[test]
    fn replace_force_overwrites_and_reports_forced() {
        let body = "тело";
        let text = format!("{}\n{body}\n{}\n", begin("a", 1, body), end("a"));
        let p = patch(
            &text,
            vec![BlockOp::Replace {
                id: "a".into(),
                guard: HashGuard::Force,
                content: "человек сказал".into(),
                source: None,
                upsert: false,
                anchor: Anchor::Eof,
            }],
            Vec::new(),
        );
        let (out, rep) = apply(&text, &p).expect("apply");
        assert!(rep.changed);
        assert_eq!(rep.forced, vec!["a".to_string()]);
        assert!(out.contains("человек сказал"));
    }

    #[test]
    fn base_hash_mismatch_fails_fast() {
        let text = "# Doc\n";
        let p = Patch {
            target: PathBuf::from("<text>"),
            base_hash: HashGuard::Expect(sha("не то состояние")),
            block_ops: vec![BlockOp::Insert {
                anchor: Anchor::Eof,
                id: "a".into(),
                content: "тело".into(),
                source: None,
            }],
            section_ops: Vec::new(),
        };
        assert!(matches!(
            managed_err(apply_impl(text, &p)),
            ManagedError::BaseHashMismatch { .. }
        ));
    }

    #[test]
    fn replace_same_content_is_noop() {
        let body = "стабильное тело";
        let text = format!("{}\n{body}\n{}\n", begin("a", 1, body), end("a"));
        let p = patch(
            &text,
            vec![BlockOp::Replace {
                id: "a".into(),
                guard: HashGuard::Expect(sha(body)),
                content: body.into(),
                source: None,
                upsert: false,
                anchor: Anchor::Eof,
            }],
            Vec::new(),
        );
        let (out, rep) = apply(&text, &p).expect("apply");
        assert!(!rep.changed);
        assert!(!rep.written);
        assert_eq!(out, text);
    }

    #[test]
    fn insert_block_after_anchor_and_eof() {
        let text = format!(
            "# Doc\n\n{}\nтело a\n{}\n\nхвост\n",
            begin("a", 1, "тело a"),
            end("a")
        );
        let p = patch(
            &text,
            vec![
                BlockOp::Insert {
                    anchor: Anchor::AfterBlock("a".into()),
                    id: "b".into(),
                    content: "тело b".into(),
                    source: None,
                },
                BlockOp::Insert {
                    anchor: Anchor::Eof,
                    id: "c".into(),
                    content: "тело c".into(),
                    source: None,
                },
            ],
            Vec::new(),
        );
        let (out, _rep) = apply(&text, &p).expect("apply");
        let ids: Vec<String> = parse_blocks(&out)
            .expect("parse")
            .into_iter()
            .map(|b| b.id)
            .collect();
        assert_eq!(ids, vec!["a", "b", "c"]);
    }

    #[test]
    fn insert_existing_id_same_body_is_noop_other_body_is_duplicate() {
        let body = "тело";
        let text = format!("{}\n{body}\n{}\n", begin("a", 1, body), end("a"));

        let same = patch(
            &text,
            vec![BlockOp::Insert {
                anchor: Anchor::Eof,
                id: "a".into(),
                content: body.into(),
                source: None,
            }],
            Vec::new(),
        );
        let (out, rep) = apply(&text, &same).expect("apply");
        assert!(!rep.changed);
        assert_eq!(out, text);

        let other = patch(
            &text,
            vec![BlockOp::Insert {
                anchor: Anchor::Eof,
                id: "a".into(),
                content: "другое".into(),
                source: None,
            }],
            Vec::new(),
        );
        assert!(matches!(
            managed_err(apply_impl(&text, &other)),
            ManagedError::DuplicateId { .. }
        ));
    }

    #[test]
    fn remove_block_drops_markers_and_reports_not_found() {
        let b = render_block("a", 1, &sha("body"), None, "body");
        let text = format!("# Doc\n\n{b}\nконец\n");
        let p = patch(
            &text,
            vec![BlockOp::Remove {
                id: "a".into(),
                guard: HashGuard::Expect(sha("body")),
            }],
            Vec::new(),
        );
        let (out, rep) = apply(&text, &p).expect("apply");
        assert!(rep.changed);
        assert!(!out.contains("agent:managed"));
        assert_eq!(out, "# Doc\n\nконец\n");

        // Повторный Remove: блока нет — это ошибка, не no-op.
        let again = patch(
            &out,
            vec![BlockOp::Remove {
                id: "a".into(),
                guard: HashGuard::Force,
            }],
            Vec::new(),
        );
        assert!(matches!(
            managed_err(apply_impl(&out, &again)),
            ManagedError::BlockNotFound { .. }
        ));
    }

    #[test]
    fn section_replace_keeps_heading_replaces_body() {
        let text = "# Doc\n\n## Section\nстарое тело\n\n## Next\nдругое\n";
        let p = patch(
            text,
            Vec::new(),
            vec![SectionOp::Replace {
                anchor: vec!["Section".into()],
                content: "новое тело".into(),
            }],
        );
        let (out, rep) = apply(text, &p).expect("apply");
        assert!(rep.changed);
        assert_eq!(out, "# Doc\n\n## Section\nновое тело\n\n## Next\nдругое\n");
    }

    #[test]
    fn section_insert_child_last_then_idempotent() {
        let text = "# Doc\n\nintro\n";
        let op = || SectionOp::Insert {
            anchor: vec!["Doc".into()],
            position: Position::ChildLast,
            heading: "Child".into(),
            level: 2,
            content: "c1".into(),
        };
        let (out, rep) = apply(text, &patch(text, Vec::new(), vec![op()])).expect("apply");
        assert!(rep.changed);
        assert!(out.contains("## Child\nc1\n"));

        // Повторный прогон: дочерний заголовок уже есть → деградация в Replace → no-op.
        let (out2, rep2) = apply(&out, &patch(&out, Vec::new(), vec![op()])).expect("apply re-run");
        assert!(!rep2.changed);
        assert_eq!(out2, out);
    }

    #[test]
    fn section_append_is_idempotent_on_second_run() {
        let text = "# Doc\n\nbase\n";
        let op = || SectionOp::Append {
            anchor: vec!["Doc".into()],
            content: "- extra".into(),
        };
        let (out, rep) = apply(text, &patch(text, Vec::new(), vec![op()])).expect("apply");
        assert!(rep.changed);
        assert_eq!(out, "# Doc\n\nbase\n- extra\n");

        let (out2, rep2) = apply(&out, &patch(&out, Vec::new(), vec![op()])).expect("apply re-run");
        assert!(!rep2.changed);
        assert_eq!(out2, out);
    }

    #[test]
    fn ambiguous_heading_is_error() {
        let text = "# Doc\n\n## S\nx\n\n## S\ny\n";
        let p = patch(
            text,
            Vec::new(),
            vec![SectionOp::Replace {
                anchor: vec!["S".into()],
                content: "z".into(),
            }],
        );
        assert!(matches!(
            managed_err(apply_impl(text, &p)),
            ManagedError::AmbiguousHeading { .. }
        ));
    }

    #[test]
    fn heading_not_found_is_error() {
        let text = "# Doc\n\n## S\nx\n";
        let p = patch(
            text,
            Vec::new(),
            vec![SectionOp::Append {
                anchor: vec!["НетТакого".into()],
                content: "z".into(),
            }],
        );
        assert!(matches!(
            managed_err(apply_impl(text, &p)),
            ManagedError::HeadingNotFound { .. }
        ));
    }

    // --- I/O (tempfile) ---

    #[test]
    fn atomic_write_creates_and_replaces_file() {
        let dir = tempfile::tempdir().expect("tmp");
        let path = dir.path().join("note.md");
        atomic_write(&path, "первая").expect("write1");
        assert_eq!(std::fs::read_to_string(&path).expect("read1"), "первая");
        atomic_write(&path, "вторая").expect("write2");
        assert_eq!(std::fs::read_to_string(&path).expect("read2"), "вторая");
    }

    #[test]
    fn atomic_write_leaves_no_tmp_on_success() {
        let dir = tempfile::tempdir().expect("tmp");
        let path = dir.path().join("note.md");
        atomic_write(&path, "тело").expect("write");
        let entries: Vec<_> = std::fs::read_dir(dir.path())
            .expect("read_dir")
            .map(|e| e.expect("entry").file_name())
            .collect();
        assert_eq!(entries.len(), 1, "в каталоге ровно один файл: {entries:?}");
    }

    #[test]
    fn atomic_write_error_leaves_original_intact() {
        let dir = tempfile::tempdir().expect("tmp");
        // Родитель целевого пути — обычный файл, поэтому create_dir_all падает.
        let blocker = dir.path().join("blocker");
        std::fs::write(&blocker, "исходное").expect("seed");
        let target = blocker.join("child.md");
        assert!(atomic_write(&target, "новое").is_err());
        assert_eq!(std::fs::read_to_string(&blocker).expect("read"), "исходное");
    }

    #[test]
    fn apply_file_end_to_end_and_idempotent() {
        let dir = tempfile::tempdir().expect("tmp");
        let path = dir.path().join("AGENTS.md");
        std::fs::write(&path, "# Doc\n\nтекст\n").expect("seed");
        let base0 = std::fs::read_to_string(&path).expect("read0");

        let mk = |base: &str| Patch {
            target: path.clone(),
            base_hash: HashGuard::Expect(Sha256::of_text(base)),
            block_ops: vec![BlockOp::Insert {
                anchor: Anchor::Eof,
                id: "rules".into(),
                content: "тело правила".into(),
                source: Some("article-lib".into()),
            }],
            section_ops: Vec::new(),
        };

        let r1 = apply_file(&mk(&base0)).expect("run1");
        assert!(r1.changed && r1.written);
        let base1 = std::fs::read_to_string(&path).expect("read1");
        assert_ne!(base1, base0);
        let mtime1 = std::fs::metadata(&path)
            .expect("meta")
            .modified()
            .expect("mtime");

        let r2 = apply_file(&mk(&base1)).expect("run2");
        assert!(!r2.changed && !r2.written);
        assert_eq!(std::fs::read_to_string(&path).expect("read2"), base1);
        let mtime2 = std::fs::metadata(&path)
            .expect("meta")
            .modified()
            .expect("mtime");
        assert_eq!(mtime1, mtime2, "no-op не трогает mtime");
    }

    #[test]
    fn apply_file_hash_mismatch_does_not_touch_disk() {
        let dir = tempfile::tempdir().expect("tmp");
        let path = dir.path().join("AGENTS.md");
        let text = format!("# Doc\n\n{}\nтело\n{}\n", begin("a", 1, "тело"), end("a"));
        std::fs::write(&path, &text).expect("seed");
        let before = std::fs::read(&path).expect("bytes before");

        let p = Patch {
            target: path.clone(),
            base_hash: HashGuard::Expect(Sha256::of_text(&text)),
            block_ops: vec![BlockOp::Replace {
                id: "a".into(),
                guard: HashGuard::Expect(sha("чужой хеш")),
                content: "новое".into(),
                source: None,
                upsert: false,
                anchor: Anchor::Eof,
            }],
            section_ops: Vec::new(),
        };
        let err = apply_file(&p).expect_err("hash mismatch").to_string();
        assert!(
            err.contains("hash mismatch"),
            "ожидался текст HashMismatch, получено: {err}"
        );
        assert_eq!(std::fs::read(&path).expect("bytes after"), before);
    }

    #[test]
    fn lint_file_detects_manual_edit() {
        let dir = tempfile::tempdir().expect("tmp");
        let path = dir.path().join("AGENTS.md");
        let body = "тело";
        let clean = format!("{}\n{body}\n{}\n", begin("a", 1, body), end("a"));
        std::fs::write(&path, &clean).expect("seed");
        assert!(lint_file(&path).expect("lint clean").is_empty());

        // Правка тела без обновления hash — ровно та ручная модификация, что ловим.
        let edited = clean.replace(body, "правленое вручную");
        std::fs::write(&path, &edited).expect("edit");
        let issues = lint_file(&path).expect("lint edited");
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].rule, "hash_mismatch");
        assert_eq!(issues[0].severity, "error");
        assert_eq!(issues[0].line, 1);
    }

    #[test]
    fn lint_text_clean_file_has_no_issues() {
        let body = "тело";
        let clean = format!("# Doc\n\n{}\n{body}\n{}\n", begin("a", 1, body), end("a"));
        assert!(lint_text(&clean).is_empty());

        // Документ без блоков — предупреждение, а не ошибка.
        let issues = lint_text("# просто текст\n");
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].rule, "no_blocks");
        assert_eq!(issues[0].severity, "warn");
    }

    // --- Совместимость с примерами методологии ---

    #[test]
    fn managed_marker_grammar_matches_methodology_example() {
        let body1 =
            "## Формат карточек концепций\n- один md-файл — одно извлечение из одной статьи";
        let b1 = render_block("concept-cards", 3, &sha(body1), Some("article-lib"), body1);
        let body2 = "## Правила формата";
        let b2 = render_block("format-rules", 1, &sha(body2), Some("article-lib"), body2);
        let text = format!("# Правила проекта\n\n{b1}\n{b2}");
        let blocks = parse_blocks(&text).expect("parse");
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0].id, "concept-cards");
        assert_eq!(blocks[0].ver, 3);
        assert_eq!(blocks[0].source.as_deref(), Some("article-lib"));
        assert_eq!(blocks[1].id, "format-rules");
        // Неподвижная точка: маркеры уже каноничны.
        assert_eq!(rerender(&text), text);
    }
}
