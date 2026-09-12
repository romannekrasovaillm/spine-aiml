//! Файловые инструменты: read/write/edit/glob/grep.
//!
//! КОНТРАКТ (владелец: агент `tools`): unit-структуры [`ReadFileTool`],
//! [`WriteFileTool`], [`EditFileTool`], [`GlobTool`], [`GrepTool`],
//! реализующие [`Tool`]. Все пути резолвятся через [`ToolContext::resolve`].
//! - `read_file`: `path`, опц. `offset`/`limit` (строки), нумерация строк;
//! - `write_file`: `path`, `content`, опц. `mode` (`overwrite` по умолчанию,
//!   `append` — дозапись в конец; крупные файлы — частями по 8–12 КБ);
//! - `edit_file`: `path`, `old_string`, `new_string`, опц. `replace_all`;
//!   ошибка, если `old_string` не найден или неуникален (без `replace_all`);
//! - `glob`: `pattern` (globset-подобный, `**`), опц. `path`;
//! - `grep`: `pattern` (regex), опц. `path`, `glob`, `context` (строки ±);
//!
//! Все выводы усечены разумно (файлы — ~50 КБ, списки — ~500 строк,
//!   совпадения grep — ~200).
//!
//! Правило ошибок модуля: нарушение контракта аргументов и внутренние сбои
//! — `Err(HarnessError)`; ожидаемые сбои рантайма (нет файла, бинарный,
//! совпадений нет, невалидный regex) — `Ok(ToolOutput::err(..))`, чтобы
//! модель получила читаемый сигнал без префиксов диспетчера.

use std::borrow::Cow;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use async_trait::async_trait;
use regex::Regex;
use serde_json::{Value, json};
use walkdir::WalkDir;

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Лимит вывода `read_file`/`grep`, символов (≈50 КБ); дальше — усечение с пометкой.
const TEXT_MAX_CHARS: usize = 50 * 1024;
/// Лимит строк результата glob.
const GLOB_MAX_RESULTS: usize = 500;
/// Лимит совпадений grep (контекстные строки в лимит не входят).
const GREP_MAX_MATCHES: usize = 200;
/// Максимум контекстных строк grep в каждую сторону.
const GREP_MAX_CONTEXT: u64 = 50;
/// Сколько первых байт файла проверять на NUL (детект бинарности).
const BINARY_SNIFF_BYTES: usize = 8 * 1024;
/// Лимит длины одной строки в выводе grep, символов.
const GREP_MAX_LINE_CHARS: usize = 500;
/// Каталоги, которые glob/grep пропускают при рекурсивном обходе.
const SKIP_DIRS: [&str; 3] = [".git", "target", "node_modules"];

/// Обязательный строковый аргумент вызова (пустой не считается).
fn req_str<'a>(args: &'a Value, tool: &str, name: &str) -> Result<&'a str> {
    args.get(name)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            HarnessError::Tool(format!(
                "{tool}: обязательный аргумент `{name}` отсутствует или не строка"
            ))
        })
}

/// Обязательный строковый аргумент, допускающий пустую строку
/// (`content` у `write_file`, `new_string` у `edit_file`).
fn req_str_allow_empty<'a>(args: &'a Value, tool: &str, name: &str) -> Result<&'a str> {
    args.get(name).and_then(Value::as_str).ok_or_else(|| {
        HarnessError::Tool(format!(
            "{tool}: обязательный аргумент `{name}` отсутствует или не строка"
        ))
    })
}

/// Опциональный непустой строковый аргумент.
fn opt_str<'a>(args: &'a Value, name: &str) -> Option<&'a str> {
    args.get(name)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
}

/// Опциональный целочисленный аргумент как usize (с насыщением).
fn opt_usize(args: &Value, name: &str) -> Option<usize> {
    args.get(name)
        .and_then(Value::as_u64)
        .map(|v| usize::try_from(v).unwrap_or(usize::MAX))
}

/// Проверяет первые байты на NUL — признак бинарного файла.
fn is_binary(bytes: &[u8]) -> bool {
    let head = &bytes[..bytes.len().min(BINARY_SNIFF_BYTES)];
    head.contains(&0)
}

/// Читает файл текстом (UTF-8, lossy).
///
/// Ошибки чтения и бинарные файлы возвращаются как готовый
/// [`ToolOutput::err`] — инструменты отдают его модели без обёрток.
async fn read_text(path: &Path) -> std::result::Result<String, ToolOutput> {
    let bytes = match tokio::fs::read(path).await {
        Ok(b) => b,
        Err(e) => {
            return Err(ToolOutput::err(format!(
                "не удалось прочитать {}: {e}",
                path.display()
            )));
        }
    };
    if is_binary(&bytes) {
        return Err(ToolOutput::err(format!(
            "бинарный файл: {} (чтение как текст невозможно)",
            path.display()
        )));
    }
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

/// Предикат для `WalkDir::filter_entry`: false для служебных каталогов
/// (`.git`, `target`, `node_modules`) — их поддеревья не обходятся.
fn enter_dir(entry: &walkdir::DirEntry) -> bool {
    !(entry.depth() > 0
        && entry.file_type().is_dir()
        && SKIP_DIRS.contains(&entry.file_name().to_string_lossy().as_ref()))
}

/// Сопоставляет glob-сегмент (`*` — любая последовательность символов,
/// `?` — ровно один символ) с одним сегментом пути (без разделителей).
fn segment_match(pattern: &str, name: &str) -> bool {
    let pat: Vec<char> = pattern.chars().collect();
    let txt: Vec<char> = name.chars().collect();
    let (mut p, mut t) = (0_usize, 0_usize);
    let (mut star_p, mut star_t) = (usize::MAX, 0_usize);
    while t < txt.len() {
        if p < pat.len() && (pat[p] == '?' || pat[p] == txt[t]) {
            p += 1;
            t += 1;
        } else if p < pat.len() && pat[p] == '*' {
            star_p = p;
            star_t = t;
            p += 1;
        } else if star_p != usize::MAX {
            // Откат к последней '*': пробуем захватить на символ больше.
            p = star_p + 1;
            star_t += 1;
            t = star_t;
        } else {
            return false;
        }
    }
    while p < pat.len() && pat[p] == '*' {
        p += 1;
    }
    p == pat.len()
}

/// Сопоставляет сегменты паттерна с сегментами пути; `**` — ноль или более
/// сегментов любой глубины.
fn match_segments(pat: &[&str], path: &[&str]) -> bool {
    match pat.split_first() {
        None => path.is_empty(),
        Some((&"**", rest)) => (0..=path.len()).any(|skip| match_segments(rest, &path[skip..])),
        Some((seg, rest)) => match path.split_first() {
            Some((head, tail)) => segment_match(seg, head) && match_segments(rest, tail),
            None => false,
        },
    }
}

/// Сопоставляет glob-паттерн (разделитель `/`, `**` — любая глубина)
/// с относительным путём.
fn glob_match(pattern: &str, path: &Path) -> bool {
    let pat_segs: Vec<&str> = pattern
        .split('/')
        .filter(|s| !s.is_empty() && *s != ".")
        .collect();
    let name_storage: Vec<String> = path
        .iter()
        .map(|c| c.to_string_lossy().into_owned())
        .collect();
    let names: Vec<&str> = name_storage.iter().map(String::as_str).collect();
    match_segments(&pat_segs, &names)
}

/// Усекает слишком длинную строку для вывода grep.
fn trim_line(line: &str) -> Cow<'_, str> {
    if line.chars().count() > GREP_MAX_LINE_CHARS {
        Cow::Owned(format!(
            "{}…",
            line.chars().take(GREP_MAX_LINE_CHARS).collect::<String>()
        ))
    } else {
        Cow::Borrowed(line)
    }
}

/// Инструмент `read_file`: чтение текстового файла с нумерацией строк.
///
/// Аргументы: `path` (обяз.), опц. `offset` (1-based номер первой строки,
/// дефолт 1) и `limit` (максимум строк). Вывод — строки вида `N→строка`.
/// Бинарные файлы (NUL в первых 8 КБ) отвергаются; вывод усекается до 50 КБ.
pub struct ReadFileTool;

#[async_trait]
impl Tool for ReadFileTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "read_file".into(),
            description: "Прочитать текстовый файл с нумерацией строк (формат `N→строка`). \
                Вызывать перед edit_file, чтобы увидеть точное содержимое с отступами, \
                и для просмотра кода, конфигов, логов. Для больших файлов читайте \
                порциями через offset/limit. Бинарные файлы не читаются."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к файлу; относительный — от текущего каталога харнесса"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Номер первой читаемой строки (1-based, дефолт 1)",
                        "minimum": 1
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимум строк для чтения (по умолчанию — до конца файла)",
                        "minimum": 1
                    }
                },
                "required": ["path"]
            }),
        }
    }

    /// Читает файл и нумерует строки.
    ///
    /// # Errors
    /// Возвращает `Err` только при нарушении контракта аргументов (нет
    /// `path`). Ошибки чтения и бинарные файлы — `Ok(ToolOutput::err(..))`.
    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let path = ctx.resolve(req_str(&args, "read_file", "path")?);
        let offset = opt_usize(&args, "offset").unwrap_or(1).max(1);
        let limit = opt_usize(&args, "limit");

        let text = match read_text(&path).await {
            Ok(t) => t,
            Err(out) => return Ok(out),
        };
        let lines: Vec<&str> = text.lines().collect();
        if lines.is_empty() {
            return Ok(ToolOutput::ok(format!("(пустой файл: {})", path.display())));
        }
        if offset > lines.len() {
            return Ok(ToolOutput::err(format!(
                "read_file: offset {offset} за пределами {} (всего {} строк)",
                path.display(),
                lines.len()
            )));
        }
        let end = limit.map_or(lines.len(), |l| {
            (offset - 1).saturating_add(l).min(lines.len())
        });
        let mut content = String::new();
        for (idx, line) in lines[offset - 1..end].iter().enumerate() {
            let _ = writeln!(content, "{}→{}", offset + idx, line);
        }
        Ok(ToolOutput::ok(content).truncated(TEXT_MAX_CHARS))
    }
}

/// Инструмент `write_file`: создание/полная перезапись файла.
///
/// Аргументы: `path`, `content` (оба обяз.). Родительские каталоги
/// создаются автоматически. Для точечной правки существующего файла
/// предназначен [`EditFileTool`].
pub struct WriteFileTool;

#[async_trait]
impl Tool for WriteFileTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "write_file".into(),
            description: "Создать или полностью перезаписать файл (родительские каталоги \
                создаются автоматически). Вызывать для новых файлов и полной перезаписи; \
                для точечной правки существующего файла — edit_file. Крупное содержимое \
                пишите ЧАСТЯМИ по 8–12 КБ: первый вызов — mode=\"overwrite\", далее — \
                mode=\"append\"; гигантский одноразовый вызов упирается в потолок \
                max_tokens и обрезается на середине."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к файлу; относительный — от текущего каталога харнесса"
                    },
                    "content": {
                        "type": "string",
                        "description": "Содержимое (целиком при overwrite, порция при append)"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "overwrite (по умолчанию) — перезаписать файл; append — дописать в конец"
                    }
                },
                "required": ["path", "content"]
            }),
        }
    }

    /// Пишет файл, создавая родительские каталоги.
    ///
    /// # Errors
    /// Возвращает `Err` только при нарушении контракта аргументов.
    /// Ошибки записи — `Ok(ToolOutput::err(..))`.
    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let path = ctx.resolve(req_str(&args, "write_file", "path")?);
        let content = req_str_allow_empty(&args, "write_file", "content")?;
        let append = opt_str(&args, "mode").is_some_and(|m| m == "append");

        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                if let Err(e) = tokio::fs::create_dir_all(parent).await {
                    return Ok(ToolOutput::err(format!(
                        "write_file: не удалось создать каталог {}: {e}",
                        parent.display()
                    )));
                }
            }
        }
        let outcome = if append {
            use tokio::io::AsyncWriteExt as _;
            match tokio::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&path)
                .await
            {
                Ok(mut f) => match f.write_all(content.as_bytes()).await {
                    Ok(()) => {
                        // `poll_write` у tokio `File` возвращает Ready сразу после
                        // постановки blocking-задачи — `write_all` НЕ гарантирует,
                        // что байты уже в ядре; `flush` дожидается inflight-записи
                        // и возвращает её результат (иначе «успех» расходился бы
                        // с содержимым файла при немедленном чтении следом).
                        f.flush().await
                    }
                    Err(e) => Err(e),
                },
                Err(e) => Err(e),
            }
        } else {
            tokio::fs::write(&path, content).await
        };
        if let Err(e) = outcome {
            return Ok(ToolOutput::err(format!(
                "write_file: не удалось записать {}: {e}",
                path.display()
            )));
        }
        Ok(ToolOutput::ok(format!(
            "{} {} байт в {}",
            if append {
                "дописано"
            } else {
                "записано"
            },
            content.len(),
            path.display()
        )))
    }
}

/// Инструмент `edit_file`: точечная замена `old_string` → `new_string`.
///
/// Без `replace_all` фрагмент обязан встречаться ровно один раз. Совпадение
/// — байт-в-байт, включая отступы и переносы строк. Успех возвращает
/// краткий итог: число заменённых вхождений и объём фрагмента в строках.
pub struct EditFileTool;

#[async_trait]
impl Tool for EditFileTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "edit_file".into(),
            description: "Точечная замена фрагмента текста: old_string → new_string. \
                Вызывать для правок существующих файлов. Совпадение — каскад: сначала \
                точное (байт-в-байт), затем терпимое к хвостовым/краевым пробелам и \
                сдвоенным пробелам (построчно). Без replace_all=true фрагмент обязан \
                быть уникальным в файле."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к файлу; относительный — от текущего каталога харнесса"
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Точный фрагмент для замены (непустой)"
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Новый фрагмент (пустая строка удаляет old_string)"
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Заменить все вхождения, а не только единственное",
                        "default": false
                    }
                },
                "required": ["path", "old_string", "new_string"]
            }),
        }
    }

    /// Выполняет замену с проверками уникальности.
    ///
    /// # Errors
    /// Возвращает `Err` только при нарушении контракта аргументов.
    /// «Не найден»/«неуникален»/ошибки записи — `Ok(ToolOutput::err(..))`.
    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let path = ctx.resolve(req_str(&args, "edit_file", "path")?);
        let old = req_str(&args, "edit_file", "old_string")?;
        let new = req_str_allow_empty(&args, "edit_file", "new_string")?;
        let replace_all = args
            .get("replace_all")
            .and_then(Value::as_bool)
            .unwrap_or(false);

        let text = match read_text(&path).await {
            Ok(t) => t,
            Err(out) => return Ok(out),
        };
        // Каскад матчеров: точное совпадение → нечёткие уровни (дрейф
        // пробелов/отступов у модели — обычное дело); контракт уникальности
        // прежний: без replace_all вхождение обязано быть единственным.
        let (replaced, level, done) =
            match crate::matchers::cascade_replace(&text, old, new, replace_all) {
                Ok(ok) => ok,
                Err(hint) => {
                    return Ok(ToolOutput::err(format!(
                        "edit_file: `old_string` в {}: {hint}",
                        path.display()
                    )));
                }
            };
        if let Err(e) = tokio::fs::write(&path, &replaced).await {
            return Ok(ToolOutput::err(format!(
                "edit_file: не удалось записать {}: {e}",
                path.display()
            )));
        }
        let old_lines = old.lines().count().max(1);
        let new_lines = if new.is_empty() {
            0
        } else {
            new.lines().count().max(1)
        };
        Ok(ToolOutput::ok(format!(
            "edit_file: {} — заменено {done} вхождений ({old_lines} строк → {new_lines} строк; {})",
            path.display(),
            crate::matchers::level_note(level, done)
        )))
    }
}

/// Инструмент `glob`: поиск файлов по glob-паттерну.
///
/// Паттерн относителен к стартовому каталогу (`path`, дефолт —
/// [`ToolContext::cwd`]): `**` — любая глубина, `*` — любая
/// последовательность внутри имени, `?` — один символ. Каталоги `.git`,
/// `target`, `node_modules` пропускаются. Результат — не более 500 путей,
/// сортировка по времени изменения (свежие первыми).
pub struct GlobTool;

#[async_trait]
impl Tool for GlobTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "glob".into(),
            description: "Найти файлы по glob-паттерну (`**` — любая глубина, `*` — внутри \
                имени, `?` — один символ). Вызывать для поиска файлов по имени/расширению \
                (например `**/*.rs`); поиск по содержимому — grep. Свежие файлы первыми, \
                максимум 500 результатов; .git/target/node_modules пропускаются."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob-паттерн относительно стартового каталога (поддерживаются **, *, ?)"
                    },
                    "path": {
                        "type": "string",
                        "description": "Стартовый каталог (дефолт — текущий каталог харнесса)"
                    }
                },
                "required": ["pattern"]
            }),
        }
    }

    /// Обходит каталог в `spawn_blocking` и фильтрует пути матчером.
    ///
    /// # Errors
    /// Возвращает `Err` при нарушении контракта аргументов или срыве
    /// блокирующей задачи. Отсутствие каталога — `Ok(ToolOutput::err(..))`.
    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let pattern = req_str(&args, "glob", "pattern")?.to_owned();
        let start = opt_str(&args, "path").map_or_else(|| ctx.cwd.clone(), |p| ctx.resolve(p));

        let out = tokio::task::spawn_blocking(move || glob_run(&start, &pattern))
            .await
            .map_err(|e| HarnessError::Tool(format!("glob: задача обхода прервана: {e}")))?;
        Ok(out)
    }
}

/// Синхронная часть [`GlobTool`]: обход, матчинг, сортировка по mtime убыв.
fn glob_run(start: &Path, pattern: &str) -> ToolOutput {
    if !start.is_dir() {
        return ToolOutput::err(format!(
            "glob: стартовый каталог не существует: {}",
            start.display()
        ));
    }
    let mut hits: Vec<(PathBuf, SystemTime)> = Vec::new();
    for entry in WalkDir::new(start)
        .into_iter()
        .filter_entry(enter_dir)
        .filter_map(std::result::Result::ok)
    {
        if !entry.file_type().is_file() {
            continue;
        }
        let Ok(rel) = entry.path().strip_prefix(start) else {
            continue;
        };
        if glob_match(pattern, rel) {
            let mtime = entry
                .metadata()
                .ok()
                .and_then(|m| m.modified().ok())
                .unwrap_or(SystemTime::UNIX_EPOCH);
            hits.push((entry.path().to_path_buf(), mtime));
        }
    }
    hits.sort_by_key(|h| std::cmp::Reverse(h.1));
    let total = hits.len();
    hits.truncate(GLOB_MAX_RESULTS);
    if hits.is_empty() {
        return ToolOutput::ok(format!(
            "glob: совпадений нет (паттерн `{pattern}`, каталог {})",
            start.display()
        ));
    }
    let mut content = String::new();
    for (path, _) in &hits {
        let _ = writeln!(content, "{}", path.display());
    }
    if total > GLOB_MAX_RESULTS {
        let _ = writeln!(
            content,
            "… [усечено: показаны {GLOB_MAX_RESULTS} из {total}]"
        );
    }
    ToolOutput::ok(content)
}

/// Инструмент `grep`: поиск по содержимому файлов (regex, крейт `regex`).
///
/// Аргументы: `pattern` (обяз.), опц. `path` (файл или каталог, дефолт —
/// [`ToolContext::cwd`]), `glob` (фильтр имён файлов, напр. `*.rs`),
/// `context` (строки ± вокруг совпадения, дефолт 0, макс 50). Вывод —
/// `path:строка: текст` для совпадений и `path-строка- текст` для
/// контекста; не более 200 совпадений. Бинарные файлы пропускаются.
pub struct GrepTool;

#[async_trait]
impl Tool for GrepTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "grep".into(),
            description: "Найти строки по regex в файле или рекурсивно в каталоге. \
                Вызывать для поиска по содержимому: определения функций и типов, вызовы, \
                TODO, ключи конфигов. glob фильтрует имена файлов (`*.rs`), context \
                добавляет строки до/после совпадения. Максимум 200 совпадений; \
                бинарные файлы и .git/target/node_modules пропускаются."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Регулярное выражение (синтаксис крейта regex)"
                    },
                    "path": {
                        "type": "string",
                        "description": "Файл или каталог для поиска (дефолт — текущий каталог харнесса)"
                    },
                    "glob": {
                        "type": "string",
                        "description": "Фильтр имён файлов, напр. `*.rs` или `Cargo.toml`"
                    },
                    "context": {
                        "type": "integer",
                        "description": "Строк контекста до и после совпадения (дефолт 0, макс 50)",
                        "default": 0,
                        "minimum": 0,
                        "maximum": GREP_MAX_CONTEXT
                    }
                },
                "required": ["pattern"]
            }),
        }
    }

    /// Ищет совпадения в `spawn_blocking`.
    ///
    /// # Errors
    /// Возвращает `Err` при нарушении контракта аргументов или срыве
    /// блокирующей задачи. Невалидный regex и отсутствие пути —
    /// `Ok(ToolOutput::err(..))`.
    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let pattern = req_str(&args, "grep", "pattern")?;
        let re = match Regex::new(pattern) {
            Ok(re) => re,
            Err(e) => {
                return Ok(ToolOutput::err(format!(
                    "grep: невалидный regex `{pattern}`: {e}"
                )));
            }
        };
        let root = opt_str(&args, "path").map_or_else(|| ctx.cwd.clone(), |p| ctx.resolve(p));
        let name_filter = opt_str(&args, "glob").map(str::to_owned);
        let context = args
            .get("context")
            .and_then(Value::as_u64)
            .unwrap_or(0)
            .min(GREP_MAX_CONTEXT);
        let context = usize::try_from(context).unwrap_or(0);

        let out = tokio::task::spawn_blocking(move || {
            grep_run(&root, &re, name_filter.as_deref(), context)
        })
        .await
        .map_err(|e| HarnessError::Tool(format!("grep: задача поиска прервана: {e}")))?;
        Ok(out)
    }
}

/// Синхронная часть [`GrepTool`]: обход файлов и построчный поиск.
fn grep_run(root: &Path, re: &Regex, name_filter: Option<&str>, context: usize) -> ToolOutput {
    let mut files: Vec<PathBuf> = Vec::new();
    if root.is_file() {
        files.push(root.to_path_buf());
    } else if root.is_dir() {
        for entry in WalkDir::new(root)
            .into_iter()
            .filter_entry(enter_dir)
            .filter_map(std::result::Result::ok)
        {
            if !entry.file_type().is_file() {
                continue;
            }
            if let Some(filter) = name_filter {
                let name = entry.file_name().to_string_lossy();
                if !segment_match(filter, &name) {
                    continue;
                }
            }
            files.push(entry.path().to_path_buf());
        }
        files.sort();
    } else {
        return ToolOutput::err(format!("grep: путь не существует: {}", root.display()));
    }

    let mut out = String::new();
    let mut matches = 0_usize;
    let mut over_limit = false;
    'files: for file in &files {
        let Ok(bytes) = std::fs::read(file) else {
            continue;
        };
        if is_binary(&bytes) {
            continue;
        }
        let text = String::from_utf8_lossy(&bytes);
        let lines: Vec<&str> = text.lines().collect();
        let mut last_printed: Option<usize> = None;
        for (i, line) in lines.iter().enumerate() {
            if !re.is_match(line) {
                continue;
            }
            matches += 1;
            if matches > GREP_MAX_MATCHES {
                over_limit = true;
                break 'files;
            }
            let lo = i.saturating_sub(context);
            let hi = (i + context).min(lines.len().saturating_sub(1));
            // Разделитель между несмежными группами контекста — как у grep.
            if let Some(lp) = last_printed {
                if lo > lp + 1 {
                    out.push_str("--\n");
                }
            }
            let from = match last_printed {
                Some(lp) if lo <= lp => lp + 1,
                _ => lo,
            };
            for (j, ctx_line) in lines.iter().enumerate().take(hi + 1).skip(from) {
                let marker = if j == i { ':' } else { '-' };
                let _ = writeln!(
                    out,
                    "{}{marker}{}{marker} {}",
                    file.display(),
                    j + 1,
                    trim_line(ctx_line)
                );
                last_printed = Some(j);
            }
        }
    }
    if matches == 0 {
        return ToolOutput::ok(format!("grep: совпадений не найдено в {}", root.display()));
    }
    if over_limit {
        let _ = writeln!(
            out,
            "… [усечено: показаны первые {GREP_MAX_MATCHES} совпадений]"
        );
    }
    ToolOutput::ok(out).truncated(TEXT_MAX_CHARS)
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use tempfile::TempDir;

    use super::*;
    use crate::config::Config;

    fn test_ctx(dir: &TempDir) -> ToolContext {
        ToolContext::new(dir.path().to_path_buf(), Arc::new(Config::default()))
    }

    fn abs(dir: &TempDir, rel: &str) -> String {
        dir.path().join(rel).to_string_lossy().into_owned()
    }

    #[tokio::test]
    async fn write_then_read_roundtrip() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        let w = WriteFileTool
            .call(
                json!({"path": "sub/note.txt", "content": "привет\nмир\n"}),
                &ctx,
            )
            .await?;
        assert!(!w.is_error, "output: {}", w.content);
        assert!(w.content.contains("записано"), "output: {}", w.content);
        assert!(w.content.contains("sub/note.txt"), "output: {}", w.content);

        let r = ReadFileTool
            .call(json!({"path": "sub/note.txt"}), &ctx)
            .await?;
        assert!(!r.is_error, "output: {}", r.content);
        assert!(r.content.contains("1→привет"), "output: {}", r.content);
        assert!(r.content.contains("2→мир"), "output: {}", r.content);
        Ok(())
    }

    #[tokio::test]
    async fn write_file_append_accumulates_chunks() -> Result<()> {
        // Чанкованная запись крупных файлов: первая порция — overwrite,
        // следующие — append (сценарий восстановления после усечения
        // гигантского вызова потолком max_tokens).
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        let w1 = WriteFileTool
            .call(json!({"path": "big/gen.py", "content": "part1\n"}), &ctx)
            .await?;
        assert!(!w1.is_error, "output: {}", w1.content);
        let w2 = WriteFileTool
            .call(
                json!({"path": "big/gen.py", "content": "part2\n", "mode": "append"}),
                &ctx,
            )
            .await?;
        assert!(!w2.is_error, "output: {}", w2.content);
        assert!(w2.content.contains("дописано"), "output: {}", w2.content);
        assert_eq!(
            std::fs::read_to_string(dir.path().join("big/gen.py"))?,
            "part1\npart2\n"
        );
        // Без mode — полная перезапись (поведение по умолчанию сохранено).
        let w3 = WriteFileTool
            .call(json!({"path": "big/gen.py", "content": "final\n"}), &ctx)
            .await?;
        assert!(w3.content.contains("записано"), "output: {}", w3.content);
        assert_eq!(
            std::fs::read_to_string(dir.path().join("big/gen.py"))?,
            "final\n"
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_honours_offset_and_limit() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        std::fs::write(dir.path().join("f.txt"), "l1\nl2\nl3\nl4\nl5\n")?;

        let r = ReadFileTool
            .call(json!({"path": "f.txt", "offset": 2, "limit": 2}), &ctx)
            .await?;
        assert!(r.content.contains("2→l2"), "output: {}", r.content);
        assert!(r.content.contains("3→l3"), "output: {}", r.content);
        assert!(!r.content.contains("1→l1"), "output: {}", r.content);
        assert!(!r.content.contains("4→l4"), "output: {}", r.content);

        let beyond = ReadFileTool
            .call(json!({"path": "f.txt", "offset": 99}), &ctx)
            .await?;
        assert!(beyond.is_error, "output: {}", beyond.content);
        Ok(())
    }

    #[tokio::test]
    async fn read_rejects_binary_file() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        std::fs::write(dir.path().join("bin.dat"), [0xFFu8, 0x00, 0x01])?;

        let r = ReadFileTool.call(json!({"path": "bin.dat"}), &ctx).await?;
        assert!(r.is_error, "output: {}", r.content);
        assert!(r.content.contains("бинарный"), "output: {}", r.content);
        Ok(())
    }

    #[tokio::test]
    async fn edit_replaces_unique_fragment() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        std::fs::write(dir.path().join("a.txt"), "alpha beta gamma\n")?;

        let e = EditFileTool
            .call(
                json!({"path": "a.txt", "old_string": "beta", "new_string": "BETA"}),
                &ctx,
            )
            .await?;
        assert!(!e.is_error, "output: {}", e.content);
        assert!(e.content.contains("заменено 1"), "output: {}", e.content);
        let after = std::fs::read_to_string(dir.path().join("a.txt"))?;
        assert_eq!(after, "alpha BETA gamma\n");
        Ok(())
    }

    #[tokio::test]
    async fn edit_nonunique_requires_replace_all() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        std::fs::write(dir.path().join("d.txt"), "dup x dup\n")?;

        let e = EditFileTool
            .call(
                json!({"path": "d.txt", "old_string": "dup", "new_string": "D"}),
                &ctx,
            )
            .await?;
        assert!(e.is_error, "output: {}", e.content);
        assert!(e.content.contains("2 раз"), "output: {}", e.content);

        let all = EditFileTool
            .call(
                json!({"path": "d.txt", "old_string": "dup", "new_string": "D", "replace_all": true}),
                &ctx,
            )
            .await?;
        assert!(!all.is_error, "output: {}", all.content);
        assert!(
            all.content.contains("заменено 2"),
            "output: {}",
            all.content
        );
        let after = std::fs::read_to_string(dir.path().join("d.txt"))?;
        assert_eq!(after, "D x D\n");
        Ok(())
    }

    #[tokio::test]
    async fn edit_missing_fragment_errors_with_hint() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        std::fs::write(dir.path().join("m.txt"), "some content\n")?;

        let e = EditFileTool
            .call(
                json!({"path": "m.txt", "old_string": "нет такого", "new_string": "x"}),
                &ctx,
            )
            .await?;
        assert!(e.is_error, "output: {}", e.content);
        assert!(e.content.contains("не найден"), "output: {}", e.content);
        Ok(())
    }

    #[tokio::test]
    async fn glob_finds_rs_files_recursively() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        std::fs::create_dir_all(dir.path().join("src/deep"))?;
        std::fs::create_dir_all(dir.path().join("target"))?;
        std::fs::write(dir.path().join("a.rs"), "fn a() {}\n")?;
        std::fs::write(dir.path().join("src/b.rs"), "fn b() {}\n")?;
        std::fs::write(dir.path().join("src/deep/c.rs"), "fn c() {}\n")?;
        std::fs::write(dir.path().join("d.txt"), "text\n")?;
        std::fs::write(dir.path().join("target/e.rs"), "fn e() {}\n")?;

        let g = GlobTool.call(json!({"pattern": "**/*.rs"}), &ctx).await?;
        assert!(!g.is_error, "output: {}", g.content);
        assert!(g.content.contains("a.rs"), "output: {}", g.content);
        assert!(g.content.contains("b.rs"), "output: {}", g.content);
        assert!(g.content.contains("c.rs"), "output: {}", g.content);
        assert!(!g.content.contains("d.txt"), "output: {}", g.content);
        // target/ пропускается при обходе.
        assert!(!g.content.contains("target"), "output: {}", g.content);

        // Односегментный паттерн матчит только непосредственных детей.
        let top = GlobTool.call(json!({"pattern": "*.rs"}), &ctx).await?;
        assert!(top.content.contains("a.rs"), "output: {}", top.content);
        assert!(!top.content.contains("b.rs"), "output: {}", top.content);
        Ok(())
    }

    #[tokio::test]
    async fn grep_with_context_lines() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        std::fs::write(dir.path().join("s.txt"), "one\ntwo\nthree\nfour\nfive\n")?;

        let g = GrepTool
            .call(json!({"pattern": "three", "context": 1}), &ctx)
            .await?;
        assert!(!g.is_error, "output: {}", g.content);
        assert!(g.content.contains(":3: three"), "output: {}", g.content);
        assert!(g.content.contains("-2- two"), "output: {}", g.content);
        assert!(g.content.contains("-4- four"), "output: {}", g.content);
        assert!(!g.content.contains("-1- one"), "output: {}", g.content);
        Ok(())
    }

    #[tokio::test]
    async fn grep_glob_filter_restricts_files() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        std::fs::write(dir.path().join("x.rs"), "hit\n")?;
        std::fs::write(dir.path().join("x.txt"), "hit\n")?;

        let g = GrepTool
            .call(json!({"pattern": "hit", "glob": "*.rs"}), &ctx)
            .await?;
        assert!(g.content.contains("x.rs"), "output: {}", g.content);
        assert!(!g.content.contains("x.txt"), "output: {}", g.content);
        Ok(())
    }

    #[tokio::test]
    async fn grep_invalid_regex_is_err() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        let g = GrepTool.call(json!({"pattern": "["}), &ctx).await?;
        assert!(g.is_error, "output: {}", g.content);
        assert!(
            g.content.contains("невалидный regex"),
            "output: {}",
            g.content
        );
        Ok(())
    }

    #[test]
    fn glob_matcher_basics() {
        assert!(segment_match("*.rs", "main.rs"));
        assert!(!segment_match("*.rs", "main.rs.bak"));
        assert!(segment_match("mod?.rs", "mod1.rs"));
        assert!(!segment_match("mod?.rs", "mod12.rs"));
        assert!(segment_match("*", "anything"));

        let m = |pat: &str, p: &str| glob_match(pat, Path::new(p));
        assert!(m("**/*.rs", "a/b/c.rs"));
        assert!(m("**/*.rs", "c.rs"));
        assert!(m("src/**", "src/a/b.txt"));
        assert!(m("src/*.rs", "src/lib.rs"));
        assert!(!m("src/*.rs", "src/x/lib.rs"));
        assert!(!m("*.rs", "src/lib.rs"));
        assert!(m("**", "any/thing.txt"));
    }

    #[tokio::test]
    async fn write_into_explicit_absolute_path() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let ctx = test_ctx(&dir);
        let target = abs(&dir, "deep/nested/f.md");
        let w = WriteFileTool
            .call(json!({"path": target, "content": "# doc\n"}), &ctx)
            .await?;
        assert!(!w.is_error, "output: {}", w.content);
        assert!(
            w.content.contains("записано 6 байт"),
            "output: {}",
            w.content
        );
        Ok(())
    }
}
