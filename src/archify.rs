//! Archify — контур диаграмм «архитектура как код» (typed JSON IR → HTML/SVG).
//!
//! КОНТРАКТ (владелец: агент `archify`): тонкая обёртка над Node.js CLI
//! Archify (`bin/archify.mjs`, github.com/tt-a1i/archify): агент пишет
//! типизированный JSON IR (architecture/workflow/sequence/dataflow/lifecycle),
//! CLI детерминистично компилирует его в self-contained HTML с валидацией
//! (9 artifact checks + composition-профиль standard|showcase), доставкой
//! с SHA-256 receipt (`deliver`) и сравнением architecture-снапшотов
//! (`compare` — Before/Delta/After с машинным receipt).
//!
//! Механика здесь (AD-1): запуск процесса `node <cli>` с таймаутом, scrub
//! окружения от секретов (AD-3, та же политика `[bash] env_scrub`), разбор
//! JSON-receipt в компактную сводку для модели. Методика авторинга IR —
//! в плагине `ru-archify` (знания — в плагинах). Новых crate-зависимостей
//! нет (fitness no-new-dependencies): процесс + `serde_json`.
//!
//! Провал CLI (ненулевой exit, таймаут) — `ToolOutput::err` с разобранными
//! диагностиками (петля ремонта модели), не падение хода. Личные пути
//! (`cli_path`, `node_bin`) живут только в пользовательском конфиге.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
// fmt::Write для writeln! в String (инфаллибилен, см. summarize_receipt).
use std::fmt::Write as _;

use async_trait::async_trait;
use serde_json::Value;

use crate::config::Config;
use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Таймаут одного вызова Archify CLI по умолчанию, сек.
pub const DEFAULT_TIMEOUT_SECS: u64 = 120;
/// Верхняя граница таймаута вызова, сек (compare на больших снапшотах).
pub const MAX_TIMEOUT_SECS: u64 = 900;
/// Лимит сводки для модели, символов (полный JSON остаётся в файлах-артefактах).
const MAX_OUTPUT_CHARS: usize = 24_000;

/// Версия Archify CLI, с которой харнесс проходил живые проверки
/// (вендоренная копия `vendor/archify/`, контракт receipt `schemaVersion: 1`).
/// Обновление вендоренного движка = bump этой константы + повторная волна
/// проверки дистрибутива (deploy-kit/ПРОВЕРКА.md).
pub const PINNED_CLI_VERSION: &str = "2.17.0-dev.1";

/// Итог сверки версии установленного Archify CLI с пином [`PINNED_CLI_VERSION`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CliVersionVerdict {
    /// Версия совпала с проверенной.
    Pinned(String),
    /// Версия прочитана, но отличается от проверенной.
    Mismatch(String),
    /// Версию определить не удалось (нештатная установка без метаданных).
    Unknown,
}

/// Читает версию установленного Archify CLI: `<root>/skill-release.json`
/// (поле `version`), затем `<root>/package.json`; `<root>` — родитель
/// каталога `bin/`, где лежит `archify.mjs`.
#[must_use]
pub fn cli_version(cli_path: &Path) -> Option<String> {
    let root = cli_path.parent()?.parent()?;
    for name in ["skill-release.json", "package.json"] {
        let Ok(text) = std::fs::read_to_string(root.join(name)) else {
            // Нет файла метаданных — пробуем следующий источник версии.
            continue;
        };
        let Ok(v) = serde_json::from_str::<Value>(&text) else {
            // Битый JSON не считается источником версии.
            continue;
        };
        if let Some(ver) = v.get("version").and_then(Value::as_str) {
            return Some(ver.to_owned());
        }
    }
    None
}

/// Сверяет версию установленного CLI с пином (чистая функция для doctor).
#[must_use]
pub fn check_cli_version(cli_path: &Path) -> CliVersionVerdict {
    match cli_version(cli_path) {
        Some(v) if v == PINNED_CLI_VERSION => CliVersionVerdict::Pinned(v),
        Some(v) => CliVersionVerdict::Mismatch(v),
        None => CliVersionVerdict::Unknown,
    }
}
/// Максимум диагностик в сводке — дальше усечение со счётчиком.
const MAX_DIAGNOSTICS: usize = 12;

/// Допустимые типы диаграмм Archify.
pub const DIAGRAM_TYPES: [&str; 5] = [
    "architecture",
    "workflow",
    "sequence",
    "dataflow",
    "lifecycle",
];

/// Допустимые composition-профили качества.
pub const QUALITY_PROFILES: [&str; 2] = ["standard", "showcase"];

/// Итог одного вызова Archify CLI.
#[derive(Debug)]
pub struct CliRun {
    /// Код выхода (None — процесс не завершился: таймаут).
    pub status: Option<i32>,
    /// Вызов прерван по таймауту.
    pub timed_out: bool,
    /// stdout процесса.
    pub stdout: String,
    /// stderr процесса.
    pub stderr: String,
}

impl CliRun {
    /// Успешное завершение (exit 0, без таймаута).
    #[must_use]
    pub fn ok(&self) -> bool {
        !self.timed_out && self.status == Some(0)
    }
}

/// Резолвит исполняемую пару (node, путь к archify CLI) из конфига.
///
/// # Errors
/// [`HarnessError::Archify`] с инструкцией по установке, если CLI не
/// настроен или файл не существует (личный путь — только в конфиге).
pub fn resolve_cli(cfg: &Config) -> Result<(String, PathBuf)> {
    let node = cfg.archify.node_bin.trim();
    if node.is_empty() {
        return Err(HarnessError::Archify(
            "[archify].node_bin пуст — укажите исполняемый файл Node.js (>=18)".into(),
        ));
    }
    let cli = &cfg.archify.cli_path;
    if cli.as_os_str().is_empty() {
        return Err(HarnessError::Archify(
            "Archify не настроен: укажите в ~/.config/arch-ml/config.toml секцию \
             [archify] с cli_path = \"/путь/к/archify/bin/archify.mjs\" \
             (движок — вендоренный: archify-vendored-*.tar.gz из релиза дистрибутива \
             или vendor/archify/ в исходном репозитории; из сети npx skills add \
             tt-a1i/archify — только fallback, ставит непроверенный latest)"
                .into(),
        ));
    }
    if !cli.is_file() {
        return Err(HarnessError::Archify(format!(
            "[archify].cli_path указывает на несуществующий файл: {} — проверьте путь \
             к bin/archify.mjs установленного Archify",
            cli.display()
        )));
    }
    Ok((node.to_owned(), cli.clone()))
}

/// Запускает `node <archify-cli> <args…>` в `workdir` с таймаутом и
/// scrubbed-окружением (секреты харнесса процессу не достаются, AD-3).
///
/// Частичный вывод при таймауте не сохраняется (в отличие от `bash`):
/// receipt Archify — компактный JSON, ценности обрезка несёт мало.
///
/// # Errors
/// [`HarnessError::Archify`] — CLI не настроен;
/// [`HarnessError::Tool`] — процесс не запустился/ошибка ожидания.
pub async fn run(
    cfg: &Config,
    workdir: &Path,
    args: &[String],
    timeout_secs: u64,
) -> Result<CliRun> {
    let (node, cli) = resolve_cli(cfg)?;
    let timeout_secs = timeout_secs.clamp(1, MAX_TIMEOUT_SECS);

    let mut cmd = tokio::process::Command::new(&node);
    cmd.arg(&cli)
        .args(args)
        .current_dir(workdir)
        .kill_on_drop(true)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    // Та же дисциплина, что у bash-инструмента: vars_os + lossy (vars()
    // паникует на не-UTF8), PWD выставит сам процесс по current_dir.
    let (vars, _dropped) = crate::tools::bash::scrub_env(
        std::env::vars_os()
            .filter(|(k, _)| k != "PWD")
            .map(|(k, v)| {
                (
                    k.to_string_lossy().into_owned(),
                    v.to_string_lossy().into_owned(),
                )
            }),
        cfg.bash.env_scrub,
        &cfg.bash.env_allow,
    );
    cmd.env_clear().envs(vars);

    let child = cmd.spawn().map_err(|e| {
        HarnessError::Tool(format!(
            "archify: не удалось запустить '{node} {}' в {}: {e}",
            cli.display(),
            workdir.display()
        ))
    })?;

    match tokio::time::timeout(Duration::from_secs(timeout_secs), child.wait_with_output()).await {
        Ok(Ok(out)) => Ok(CliRun {
            status: out.status.code(),
            timed_out: false,
            stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
        }),
        Ok(Err(e)) => Err(HarnessError::Tool(format!(
            "archify: ошибка ожидания процесса: {e}"
        ))),
        Err(_) => Ok(CliRun {
            status: None,
            timed_out: true,
            stdout: String::new(),
            stderr: String::new(),
        }),
    }
}

/// Компактная сводка JSON-receipt Archify для модели.
///
/// Разбирает общий конверт (`ok`, `error`, `checks`, `composition`,
/// `diagnostics`) и профильные поля: `deliver` (sha256 спецификации и
/// артефакта), `compare` (счётчики added/removed/changed/moved/rerouted).
/// Не-JSON stdout возвращается как есть (усечённый).
#[must_use]
pub fn summarize_receipt(command: &str, stdout: &str) -> String {
    let Ok(v) = serde_json::from_str::<Value>(stdout) else {
        return stdout.chars().take(MAX_OUTPUT_CHARS).collect();
    };
    let mut out = String::new();
    let ok = v.get("ok").and_then(Value::as_bool).unwrap_or(false);
    // write! в String неизменно успешен (fmt::Write для String не фейлится).
    let _ = writeln!(
        out,
        "archify {command}: {}",
        if ok { "ok" } else { "ПРОВАЛ" }
    );

    if let Some(checks) = v.get("checks").and_then(Value::as_array) {
        let passed = checks
            .iter()
            .filter(|c| c.get("ok").and_then(Value::as_bool).unwrap_or(false))
            .count();
        let _ = writeln!(out, "checks: {passed}/{}", checks.len());
    }
    if let Some(comp) = v.get("composition") {
        let status = comp.get("status").and_then(Value::as_str).unwrap_or("?");
        let errors = comp
            .get("summary")
            .and_then(|s| s.get("errors"))
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let warnings = comp
            .get("summary")
            .and_then(|s| s.get("warnings"))
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let _ = writeln!(
            out,
            "composition: {status} (errors {errors}, warnings {warnings})"
        );
    }
    if let Some(validation) = v.get("validation") {
        let passed = validation
            .get("checksPassed")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let total = validation
            .get("checkCount")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let errors = validation
            .get("errors")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let warnings = validation
            .get("warnings")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let _ = writeln!(
            out,
            "validation: {passed}/{total} checks, errors {errors}, warnings {warnings}"
        );
    }
    for (key, label) in [("specification", "spec"), ("artifact", "artifact")] {
        if let Some(obj) = v.get(key) {
            let sha = obj.get("sha256").and_then(Value::as_str).unwrap_or("?");
            let bytes = obj.get("bytes").and_then(Value::as_u64).unwrap_or(0);
            let _ = writeln!(out, "{label}: sha256 {sha} ({bytes} байт)");
        }
    }
    if let Some(summary) = v.get("summary") {
        let _ = writeln!(out, "delta: {}", compact_json(summary));
    }
    if let Some(error) = v.get("error").and_then(Value::as_str) {
        let _ = writeln!(
            out,
            "error: {}",
            truncate(&error.replace('\n', " | "), 1200)
        );
    }
    if let Some(diags) = v.get("diagnostics").and_then(Value::as_array) {
        let _ = writeln!(out, "diagnostics ({}):", diags.len());
        for d in diags.iter().take(MAX_DIAGNOSTICS) {
            let code = d.get("code").and_then(Value::as_str).unwrap_or("?");
            let severity = d.get("severity").and_then(Value::as_str).unwrap_or("?");
            let message = d.get("message").and_then(Value::as_str).unwrap_or("");
            let _ = writeln!(out, "- [{severity}] {code}: {}", truncate(message, 400));
            if let Some(fixes) = d.get("supportedFixes").and_then(Value::as_array) {
                let fixes: Vec<&str> = fixes.iter().filter_map(Value::as_str).collect();
                if !fixes.is_empty() {
                    let _ = writeln!(out, "  fix: {}", truncate(&fixes.join("; "), 300));
                }
            }
        }
        if diags.len() > MAX_DIAGNOSTICS {
            let _ = writeln!(out, "… ещё {}", diags.len() - MAX_DIAGNOSTICS);
        }
    }
    out
}

/// Оборачивает итог вызова в `ToolOutput`: успех — сводка, провал —
/// `ToolOutput::err` с диагностиками (петля ремонта модели).
fn outcome_output(command: &str, run: &CliRun, timeout_secs: u64) -> ToolOutput {
    if run.timed_out {
        return ToolOutput::err(format!(
            "archify {command}: таймаут {timeout_secs} сек — упростите диаграмму \
             или поднимите [archify].timeout_secs"
        ));
    }
    let summary = summarize_receipt(command, &run.stdout);
    if run.ok() {
        ToolOutput::ok(summary)
    } else {
        let mut msg = format!(
            "archify {command}: exit {}\n{summary}",
            run.status.map_or("?".to_string(), |c| c.to_string())
        );
        if !run.stderr.trim().is_empty() {
            // write! в String неизменно успешен (fmt::Write для String не фейлится).
            let _ = write!(msg, "\nstderr: {}", truncate(run.stderr.trim(), 600));
        }
        ToolOutput::err(msg)
    }
}

/// Сводный JSON в одну строку (для delta-счётчиков).
fn compact_json(v: &Value) -> String {
    truncate(&v.to_string(), 600)
}

/// Усечение строки по символам с маркером.
fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_owned();
    }
    format!("{}…", s.chars().take(max).collect::<String>())
}

/// Проверяет тип диаграммы и профиль качества; возвращает ошибку-подсказку
/// для модели при невалидных значениях.
fn check_enums(diagram_type: &str, quality: &str) -> Option<ToolOutput> {
    if !DIAGRAM_TYPES.contains(&diagram_type) {
        return Some(ToolOutput::err(format!(
            "archify: неизвестный тип '{diagram_type}' (допустимы: {})",
            DIAGRAM_TYPES.join(", ")
        )));
    }
    if !QUALITY_PROFILES.contains(&quality) {
        return Some(ToolOutput::err(format!(
            "archify: неизвестный quality '{quality}' (допустимы: {})",
            QUALITY_PROFILES.join(", ")
        )));
    }
    None
}

/// Строковый аргумент вызова или ошибка-подсказка модели.
fn str_arg<'a>(args: &'a Value, name: &str) -> std::result::Result<&'a str, ToolOutput> {
    args.get(name)
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| {
            ToolOutput::err(format!(
                "archify: обязательный аргумент '{name}' отсутствует или пуст"
            ))
        })
}

/// Резолвит входной файл IR: должен существовать.
fn input_path(ctx: &ToolContext, raw: &str) -> std::result::Result<PathBuf, ToolOutput> {
    let full = ctx.resolve(raw);
    if !full.is_file() {
        return Err(ToolOutput::err(format!(
            "archify: входной файл не найден: {} (ожидался JSON IR рядом с рабочим каталогом)",
            full.display()
        )));
    }
    Ok(full)
}

/// Резолвит выходной файл: обязан оставаться внутри рабочего каталога
/// (fail-closed, как и дисциплина путей ядра).
fn output_path(ctx: &ToolContext, raw: &str) -> std::result::Result<PathBuf, ToolOutput> {
    let full = normalize_lexically(&ctx.resolve(raw));
    if !full.starts_with(normalize_lexically(&ctx.cwd)) {
        return Err(ToolOutput::err(format!(
            "archify: выходной путь за пределами рабочего каталога: {} — укажите файл внутри {}",
            full.display(),
            ctx.cwd.display()
        )));
    }
    Ok(full)
}

/// Лексическая нормализация `.`/`..` без обращения к ФС: `canonicalize`
/// требует существования файла, а выходной HTML ещё не записан.
fn normalize_lexically(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                // У корня pop — безопасный no-op (путь после resolve абсолютный).
                let _ = out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Общие свойства схемы параметров: тип диаграммы, вход, профиль.
fn base_properties() -> serde_json::Map<String, Value> {
    let mut props = serde_json::Map::new();
    props.insert(
        "type".to_owned(),
        serde_json::json!({
            "type": "string",
            "enum": DIAGRAM_TYPES,
            "description": "Тип диаграммы Archify"
        }),
    );
    props.insert(
        "path".to_owned(),
        serde_json::json!({
            "type": "string",
            "description": "Путь к JSON IR (резолвится от рабочего каталога)"
        }),
    );
    props.insert(
        "quality".to_owned(),
        serde_json::json!({
            "type": "string",
            "enum": QUALITY_PROFILES,
            "description": "Composition-профиль приёмки (по умолчанию showcase)"
        }),
    );
    props
}

/// Инструменты домена: `archify_validate`, `archify_deliver`, `archify_show`,
/// `archify_compare`.
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(ArchifyValidateTool),
        Arc::new(ArchifyDeliverTool),
        Arc::new(ArchifyShowTool),
        Arc::new(ArchifyCompareTool),
    ]
}

/// `archify_validate`: IR → сводка 9 artifact checks + composition + диагностики.
struct ArchifyValidateTool;

#[async_trait]
impl Tool for ArchifyValidateTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "archify_validate".to_owned(),
            description: "Валидирует Archify-диаграмму (JSON IR: architecture, workflow, sequence, \
                          dataflow, lifecycle) через Node CLI: 9 artifact checks + composition-профиль. \
                          При провале возвращает машиночитаемые диагностики с supportedFixes — \
                          применяйте их для точечного ремонта IR и повторяйте вызов."
                .to_owned(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": base_properties(),
                "required": ["type", "path"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let diagram_type = match str_arg(&args, "type") {
            Ok(v) => v,
            Err(out) => return Ok(out),
        };
        let quality = args
            .get("quality")
            .and_then(Value::as_str)
            .unwrap_or("showcase");
        if let Some(out) = check_enums(diagram_type, quality) {
            return Ok(out);
        }
        let path = match str_arg(&args, "path") {
            Ok(v) => match input_path(ctx, v) {
                Ok(p) => p,
                Err(out) => return Ok(out),
            },
            Err(out) => return Ok(out),
        };
        let cli_args = vec![
            "validate".to_owned(),
            diagram_type.to_owned(),
            path.to_string_lossy().into_owned(),
            "--quality".to_owned(),
            quality.to_owned(),
            "--json".to_owned(),
        ];
        let run = match run(
            &ctx.config,
            &ctx.cwd,
            &cli_args,
            ctx.config.archify.timeout_secs,
        )
        .await
        {
            Ok(run) => run,
            // Ненастроенный CLI — подсказка модели (ToolOutput::err), не сбой вызова.
            Err(e @ HarnessError::Archify(_)) => return Ok(ToolOutput::err(e.to_string())),
            Err(e) => return Err(e),
        };
        Ok(outcome_output(
            "validate",
            &run,
            ctx.config.archify.timeout_secs,
        ))
    }
}

/// `archify_deliver`: финальная приёмка — атомарная доставка HTML + SHA-256 receipt.
struct ArchifyDeliverTool;

#[async_trait]
impl Tool for ArchifyDeliverTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "archify_deliver".to_owned(),
            description: "Финальная приёмка Archify-диаграммы: валидирует IR, атомарно записывает \
                          self-contained HTML и возвращает SHA-256 спецификации и артефакта. \
                          Ненулевой exit — неуспех: чините IR по диагностикам и повторяйте. \
                          Запускайте ПОСЛЕ чистого archify_validate. Чтобы ПОКАЗАТЬ диаграмму \
                          пользователю: откройте доставленный HTML в браузере (bash: \
                          xdg-open <output>) или дайте путь к файлу; перерисовывать IR \
                          в mermaid нельзя — это потеря оригинала."
                .to_owned(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": merge_properties(base_properties(), serde_json::json!({
                    "output": {
                        "type": "string",
                        "description": "Путь к выходному HTML (внутри рабочего каталога)"
                    }
                })),
                "required": ["type", "path", "output"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let diagram_type = match str_arg(&args, "type") {
            Ok(v) => v,
            Err(out) => return Ok(out),
        };
        let quality = args
            .get("quality")
            .and_then(Value::as_str)
            .unwrap_or("showcase");
        if let Some(out) = check_enums(diagram_type, quality) {
            return Ok(out);
        }
        let path = match str_arg(&args, "path") {
            Ok(v) => match input_path(ctx, v) {
                Ok(p) => p,
                Err(out) => return Ok(out),
            },
            Err(out) => return Ok(out),
        };
        let output = match str_arg(&args, "output") {
            Ok(v) => match output_path(ctx, v) {
                Ok(p) => p,
                Err(out) => return Ok(out),
            },
            Err(out) => return Ok(out),
        };
        let cli_args = vec![
            "deliver".to_owned(),
            diagram_type.to_owned(),
            path.to_string_lossy().into_owned(),
            output.to_string_lossy().into_owned(),
            "--quality".to_owned(),
            quality.to_owned(),
            "--json".to_owned(),
        ];
        let run = match run(
            &ctx.config,
            &ctx.cwd,
            &cli_args,
            ctx.config.archify.timeout_secs,
        )
        .await
        {
            Ok(run) => run,
            // Ненастроенный CLI — подсказка модели (ToolOutput::err), не сбой вызова.
            Err(e @ HarnessError::Archify(_)) => return Ok(ToolOutput::err(e.to_string())),
            Err(e) => return Err(e),
        };
        Ok(outcome_output(
            "deliver",
            &run,
            ctx.config.archify.timeout_secs,
        ))
    }
}

/// `archify_show`: показ диаграммы пользователю — доставка HTML + открытие
/// в браузере. Санкционированный ответ на «покажи диаграмму/архифай»:
/// одна команда вместо ручной связки deliver + xdg-open и без соблазна
/// перерисовать IR в mermaid ради вкладки чата.
struct ArchifyShowTool;

#[async_trait]
impl Tool for ArchifyShowTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "archify_show".to_owned(),
            description: "ПОКАЗЫВАЕТ Archify-диаграмму пользователю: валидирует JSON IR, атомарно \
                          доставляет self-contained HTML (SHA-256 receipt) и открывает его в \
                          браузере (xdg-open; open=false — только записать файл и вернуть путь). \
                          Единственный санкционированный способ показать Archify-диаграмму. \
                          mermaid_render для Archify IR НЕ использовать ни в каком виде — ни для \
                          показа, ни для «структуры в чат»: конвертация IR в mermaid теряет \
                          оригинал и запрещена."
                .to_owned(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": merge_properties(base_properties(), serde_json::json!({
                    "output": {
                        "type": "string",
                        "description": "Путь к выходному HTML (внутри рабочего каталога; \
                                        по умолчанию <имя-IR>.html в рабочем каталоге)"
                    },
                    "open": {
                        "type": "boolean",
                        "description": "Открыть HTML в браузере после доставки (по умолчанию true)"
                    }
                })),
                "required": ["type", "path"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let diagram_type = match str_arg(&args, "type") {
            Ok(v) => v,
            Err(out) => return Ok(out),
        };
        let quality = args
            .get("quality")
            .and_then(Value::as_str)
            .unwrap_or("showcase");
        if let Some(out) = check_enums(diagram_type, quality) {
            return Ok(out);
        }
        let path = match str_arg(&args, "path") {
            Ok(v) => match input_path(ctx, v) {
                Ok(p) => p,
                Err(out) => return Ok(out),
            },
            Err(out) => return Ok(out),
        };
        // Дисциплина путей как у deliver: явный output обязан оставаться
        // внутри рабочего каталога; дефолт — <имя-IR>.html рядом с cwd.
        let output = if let Some(raw) = args
            .get("output")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
        {
            match output_path(ctx, raw) {
                Ok(p) => p,
                Err(out) => return Ok(out),
            }
        } else {
            let stem = path
                .file_stem()
                .and_then(|s| s.to_str())
                .filter(|s| !s.is_empty())
                .unwrap_or("diagram");
            ctx.cwd.join(format!("{stem}.html"))
        };
        let open = args.get("open").and_then(Value::as_bool).unwrap_or(true);
        let cli_args = vec![
            "deliver".to_owned(),
            diagram_type.to_owned(),
            path.to_string_lossy().into_owned(),
            output.to_string_lossy().into_owned(),
            "--quality".to_owned(),
            quality.to_owned(),
            "--json".to_owned(),
        ];
        let run = match run(
            &ctx.config,
            &ctx.cwd,
            &cli_args,
            ctx.config.archify.timeout_secs,
        )
        .await
        {
            Ok(run) => run,
            // Ненастроенный CLI — подсказка модели (ToolOutput::err), не сбой вызова.
            Err(e @ HarnessError::Archify(_)) => return Ok(ToolOutput::err(e.to_string())),
            Err(e) => return Err(e),
        };
        let mut out = outcome_output("deliver", &run, ctx.config.archify.timeout_secs);
        if out.is_error {
            return Ok(out);
        }
        // writeln! в String неизменно успешен (fmt::Write для String не фейлится).
        let _ = writeln!(out.content, "HTML: {}", output.display());
        if open {
            match open_in_browser(&output) {
                Ok(()) => out
                    .content
                    .push_str("открыт в браузере пользователя (xdg-open)\n"),
                Err(e) => {
                    let _ = writeln!(
                        out.content,
                        "браузер не открылся ({e}) — откройте файл вручную"
                    );
                }
            }
        } else {
            out.content
                .push_str("open=false — файл записан, откройте вручную\n");
        }
        Ok(out)
    }
}

/// Открывает файл в браузере по умолчанию (`xdg-open`).
///
/// Ожидание завершения — в фоне: xdg-open делегирует браузеру и завершается
/// сразу, а без `wait` процесс остался бы зомби до конца сессии.
fn open_in_browser(path: &Path) -> std::result::Result<(), String> {
    let mut child = tokio::process::Command::new("xdg-open")
        .arg(path)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| format!("xdg-open не запустился: {e}"))?;
    tokio::spawn(async move {
        // Код выхода делегата на показ не влияет — файл уже доставлен.
        let _ = child.wait().await;
    });
    Ok(())
}

/// `archify_compare`: дельта двух architecture-снапшотов (Before/Delta/After).
struct ArchifyCompareTool;

#[async_trait]
impl Tool for ArchifyCompareTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "archify_compare".to_owned(),
            description: "Сравнивает два валидных architecture-снапшота Archify (base → head) и \
                          собирает страницу Before/Delta/After с машинным receipt: точные факты \
                          added/removed/changed/moved/rerouted по стабильным id. Для review \
                          архитектурных изменений до мержа. Не выводит риск/импакт — только факты."
                .to_owned(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": {
                    "base": {
                        "type": "string",
                        "description": "Путь к базовому architecture JSON IR"
                    },
                    "head": {
                        "type": "string",
                        "description": "Путь к целевому architecture JSON IR"
                    },
                    "output": {
                        "type": "string",
                        "description": "Путь к выходному delta HTML (внутри рабочего каталога)"
                    },
                    "quality": {
                        "type": "string",
                        "enum": QUALITY_PROFILES,
                        "description": "Composition-профиль приёмки (по умолчанию showcase)"
                    }
                },
                "required": ["base", "head", "output"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let quality = args
            .get("quality")
            .and_then(Value::as_str)
            .unwrap_or("showcase");
        if !QUALITY_PROFILES.contains(&quality) {
            return Ok(ToolOutput::err(format!(
                "archify: неизвестный quality '{quality}' (допустимы: {})",
                QUALITY_PROFILES.join(", ")
            )));
        }
        let base = match str_arg(&args, "base") {
            Ok(v) => match input_path(ctx, v) {
                Ok(p) => p,
                Err(out) => return Ok(out),
            },
            Err(out) => return Ok(out),
        };
        let head = match str_arg(&args, "head") {
            Ok(v) => match input_path(ctx, v) {
                Ok(p) => p,
                Err(out) => return Ok(out),
            },
            Err(out) => return Ok(out),
        };
        let output = match str_arg(&args, "output") {
            Ok(v) => match output_path(ctx, v) {
                Ok(p) => p,
                Err(out) => return Ok(out),
            },
            Err(out) => return Ok(out),
        };
        let cli_args = vec![
            "compare".to_owned(),
            "architecture".to_owned(),
            base.to_string_lossy().into_owned(),
            head.to_string_lossy().into_owned(),
            output.to_string_lossy().into_owned(),
            "--quality".to_owned(),
            quality.to_owned(),
            "--json".to_owned(),
        ];
        let run = match run(
            &ctx.config,
            &ctx.cwd,
            &cli_args,
            ctx.config.archify.timeout_secs,
        )
        .await
        {
            Ok(run) => run,
            // Ненастроенный CLI — подсказка модели (ToolOutput::err), не сбой вызова.
            Err(e @ HarnessError::Archify(_)) => return Ok(ToolOutput::err(e.to_string())),
            Err(e) => return Err(e),
        };
        Ok(outcome_output(
            "compare",
            &run,
            ctx.config.archify.timeout_secs,
        ))
    }
}

/// Дополняет карту свойств параметрами из другого JSON-объекта.
fn merge_properties(
    mut base: serde_json::Map<String, Value>,
    extra: Value,
) -> serde_json::Map<String, Value> {
    if let Value::Object(map) = extra {
        base.extend(map);
    }
    base
}

#[cfg(test)]
mod tests {
    use super::*;

    const VALIDATE_OK_JSON: &str = r#"{
        "schemaVersion": 1, "ok": true, "command": "validate", "type": "architecture",
        "checks": [{"name": "single_svg", "ok": true}, {"name": "finite_svg", "ok": true}],
        "composition": {"status": "pass", "summary": {"errors": 0, "warnings": 0}}
    }"#;

    const VALIDATE_FAIL_JSON: &str = r#"{
        "schemaVersion": 1, "ok": false, "command": "validate", "type": "architecture",
        "error": "Architecture layout validation failed:\n- ребро сквозь узел",
        "diagnostics": [
            {"code": "clean-flow/edge-through-node", "severity": "error",
             "message": "ребро e1 пересекает компонент b",
             "supportedFixes": ["adjust fromSide/toSide, set route/via"]}
        ]
    }"#;

    const DELIVER_OK_JSON: &str = r#"{
        "schemaVersion": 1, "ok": true, "command": "deliver", "type": "architecture",
        "validation": {"checksPassed": 9, "checkCount": 9, "errors": 0, "warnings": 0},
        "specification": {"sha256": "aa4bb5f0", "bytes": 5264},
        "artifact": {"sha256": "fb18c56f", "bytes": 718030}
    }"#;

    const COMPARE_OK_JSON: &str = r#"{
        "schemaVersion": 1, "ok": true, "command": "compare", "type": "architecture",
        "completeness": "complete", "proofLevel": "authored",
        "summary": {"components": {"added": 1, "removed": 1, "changed": 1, "moved": 1},
                    "connections": {"added": 1, "removed": 1, "changed": 2, "rerouted": 1}}
    }"#;

    fn cfg_with_fake_node(script: &Path, dir: &Path) -> Config {
        let mut cfg = Config::default();
        cfg.archify.cli_path = script.to_path_buf();
        cfg.archify.node_bin = "bash".to_owned();
        // Тесты изолированы от машины: конфиг целиком синтетический (AD-7).
        cfg.paths.sessions_dir = dir.join("sessions");
        cfg
    }

    #[test]
    fn summarize_validate_ok_reports_checks_and_composition() {
        let s = summarize_receipt("validate", VALIDATE_OK_JSON);
        assert!(s.contains("archify validate: ok"), "{s}");
        assert!(s.contains("checks: 2/2"), "{s}");
        assert!(
            s.contains("composition: pass (errors 0, warnings 0)"),
            "{s}"
        );
    }

    #[test]
    fn summarize_validate_fail_extracts_diagnostics_with_fixes() {
        let s = summarize_receipt("validate", VALIDATE_FAIL_JSON);
        assert!(s.contains("ПРОВАЛ"), "{s}");
        assert!(s.contains("clean-flow/edge-through-node"), "{s}");
        assert!(s.contains("supportedFixes") || s.contains("fix:"), "{s}");
        assert!(s.contains("adjust fromSide/toSide"), "{s}");
    }

    #[test]
    fn summarize_compare_reports_delta_counts() {
        let s = summarize_receipt("compare", COMPARE_OK_JSON);
        assert!(s.contains("archify compare: ok"), "{s}");
        assert!(s.contains("delta:"), "{s}");
        assert!(
            s.contains("\"added\":1") || s.contains("\"added\": 1"),
            "{s}"
        );
    }

    #[test]
    fn summarize_passthrough_for_non_json() {
        let s = summarize_receipt("doctor", "Archify is ready.\n");
        assert!(s.contains("Archify is ready."), "{s}");
    }

    #[test]
    fn resolve_cli_requires_configured_path() {
        let cfg = Config::default();
        let err = resolve_cli(&cfg).expect_err("без cli_path — ошибка");
        let msg = err.to_string();
        assert!(msg.contains("cli_path"), "{msg}");
        assert!(
            msg.contains("npx skills add"),
            "инструкция по установке: {msg}"
        );
    }

    #[test]
    fn resolve_cli_rejects_missing_file() {
        let mut cfg = Config::default();
        cfg.archify.cli_path = PathBuf::from("/no/such/archify.mjs");
        let err = resolve_cli(&cfg).expect_err("несуществующий файл");
        assert!(err.to_string().contains("несуществующий"), "{err}");
    }

    /// Макет установленного Archify: `bin/archify.mjs` + метаданные версии.
    fn fake_cli_root(files: &[(&str, &str)]) -> (tempfile::TempDir, PathBuf) {
        let tmp = tempfile::tempdir().expect("tmp");
        let bin = tmp.path().join("bin");
        std::fs::create_dir_all(&bin).expect("mkdir bin");
        let cli = bin.join("archify.mjs");
        std::fs::write(&cli, "// cli").expect("write cli");
        for (name, body) in files {
            std::fs::write(tmp.path().join(name), body).expect("write meta");
        }
        (tmp, cli)
    }

    #[test]
    fn cli_version_prefers_skill_release_json() {
        let (_tmp, cli) = fake_cli_root(&[
            (
                "skill-release.json",
                r#"{"schemaVersion": 1, "version": "2.17.0-dev.1"}"#,
            ),
            ("package.json", r#"{"version": "9.9.9"}"#),
        ]);
        assert_eq!(cli_version(&cli).as_deref(), Some("2.17.0-dev.1"));
        assert_eq!(
            check_cli_version(&cli),
            CliVersionVerdict::Pinned("2.17.0-dev.1".into())
        );
    }

    #[test]
    fn cli_version_falls_back_to_package_json() {
        let (_tmp, cli) = fake_cli_root(&[("package.json", r#"{"version": "2.16.0"}"#)]);
        assert_eq!(cli_version(&cli).as_deref(), Some("2.16.0"));
        assert_eq!(
            check_cli_version(&cli),
            CliVersionVerdict::Mismatch("2.16.0".into())
        );
    }

    #[test]
    fn cli_version_unknown_without_metadata() {
        let (_tmp, cli) = fake_cli_root(&[]);
        assert_eq!(cli_version(&cli), None);
        assert_eq!(check_cli_version(&cli), CliVersionVerdict::Unknown);
    }

    #[test]
    fn cli_version_ignores_broken_metadata() {
        let (_tmp, cli) = fake_cli_root(&[
            ("skill-release.json", "не json"),
            ("package.json", r#"{"version": "2.17.0-dev.1"}"#),
        ]);
        assert_eq!(cli_version(&cli).as_deref(), Some("2.17.0-dev.1"));
    }

    #[test]
    fn check_enums_rejects_unknown_type_and_quality() {
        assert!(check_enums("graph", "showcase").is_some());
        assert!(check_enums("architecture", "gold").is_some());
        assert!(check_enums("architecture", "showcase").is_none());
        assert!(check_enums("lifecycle", "standard").is_none());
    }

    #[test]
    fn tools_expose_four_archify_tools() {
        let ts = tools();
        let names: Vec<String> = ts.iter().map(|t| t.spec().name).collect();
        assert_eq!(
            names,
            vec![
                "archify_validate",
                "archify_deliver",
                "archify_show",
                "archify_compare"
            ]
        );
    }

    #[test]
    fn deliver_spec_teaches_show_recipe_not_mermaid() {
        // Регрессия: «покажи архифай» должен вести в открытие доставленного
        // HTML, а не в перерисовку IR в mermaid (потеря оригинала).
        let d = tools()[1].spec().description;
        assert!(d.contains("xdg-open"), "{d}");
        assert!(d.contains("mermaid"), "{d}");
    }

    #[test]
    fn show_spec_is_the_only_sanctioned_show_path() {
        // Регрессия по сессии 05.09.2026: модель, отработав deliver,
        // добавляла mermaid_render «для структуры в чат». Спека show
        // обязана закрывать и эту лазейку явным запретом.
        let d = tools()[2].spec().description;
        assert!(d.contains("Единственный санкционированный"), "{d}");
        assert!(d.contains("структуры в чат"), "{d}");
        assert!(d.contains("запрещена"), "{d}");
    }

    #[tokio::test]
    async fn tool_rejects_bad_type_before_cli() {
        let ctx = ToolContext::new(PathBuf::from("."), Arc::new(Config::default()));
        let out = tools()[0]
            .call(serde_json::json!({"type": "graph", "path": "x.json"}), &ctx)
            .await
            .unwrap();
        assert!(out.is_error);
        assert!(out.content.contains("architecture"), "{}", out.content);
    }

    #[tokio::test]
    async fn tool_reports_missing_cli_path() {
        let ctx = ToolContext::new(PathBuf::from("."), Arc::new(Config::default()));
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("ir.json"), "{}").unwrap();
        let ctx = ToolContext::new(tmp.path().to_path_buf(), ctx.config);
        let out = tools()[0]
            .call(
                serde_json::json!({"type": "architecture", "path": "ir.json"}),
                &ctx,
            )
            .await
            .unwrap();
        assert!(out.is_error);
        assert!(out.content.contains("cli_path"), "{}", out.content);
    }

    #[tokio::test]
    async fn tool_validate_ok_with_fake_node() {
        let tmp = tempfile::tempdir().unwrap();
        let script = tmp.path().join("fake-archify.mjs");
        std::fs::write(&script, format!("printf '%s' '{VALIDATE_OK_JSON}'")).unwrap();
        std::fs::write(tmp.path().join("ir.json"), "{}").unwrap();
        let cfg = cfg_with_fake_node(&script, tmp.path());
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg));
        let out = tools()[0]
            .call(
                serde_json::json!({"type": "architecture", "path": "ir.json"}),
                &ctx,
            )
            .await
            .unwrap();
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("checks: 2/2"), "{}", out.content);
    }

    #[tokio::test]
    async fn tool_validate_fail_surfaces_diagnostics() {
        let tmp = tempfile::tempdir().unwrap();
        let script = tmp.path().join("fake-archify.mjs");
        std::fs::write(
            &script,
            format!(
                "printf '%s' '{VALIDATE_FAIL_JSON}' >&2; printf '%s' '{VALIDATE_FAIL_JSON}'; exit 1"
            ),
        )
        .unwrap();
        std::fs::write(tmp.path().join("ir.json"), "{}").unwrap();
        let cfg = cfg_with_fake_node(&script, tmp.path());
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg));
        let out = tools()[0]
            .call(
                serde_json::json!({"type": "architecture", "path": "ir.json"}),
                &ctx,
            )
            .await
            .unwrap();
        assert!(out.is_error, "ожидался провал: {}", out.content);
        assert!(
            out.content.contains("clean-flow/edge-through-node"),
            "{}",
            out.content
        );
        assert!(out.content.contains("exit 1"), "{}", out.content);
    }

    #[tokio::test]
    async fn tool_deliver_rejects_output_outside_workdir() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("ir.json"), "{}").unwrap();
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(Config::default()));
        let out = tools()[1]
            .call(
                serde_json::json!({
                    "type": "architecture", "path": "ir.json", "output": "../escape.html"
                }),
                &ctx,
            )
            .await
            .unwrap();
        assert!(out.is_error);
        assert!(out.content.contains("за пределами"), "{}", out.content);
    }

    #[tokio::test]
    async fn tool_compare_ok_with_fake_node() {
        let tmp = tempfile::tempdir().unwrap();
        let script = tmp.path().join("fake-archify.mjs");
        std::fs::write(&script, format!("printf '%s' '{COMPARE_OK_JSON}'")).unwrap();
        std::fs::write(tmp.path().join("base.json"), "{}").unwrap();
        std::fs::write(tmp.path().join("head.json"), "{}").unwrap();
        let cfg = cfg_with_fake_node(&script, tmp.path());
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg));
        let out = tools()[3]
            .call(
                serde_json::json!({
                    "base": "base.json", "head": "head.json", "output": "delta.html"
                }),
                &ctx,
            )
            .await
            .unwrap();
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("delta:"), "{}", out.content);
    }

    #[tokio::test]
    async fn tool_show_ok_with_fake_node_without_browser() {
        let tmp = tempfile::tempdir().unwrap();
        let script = tmp.path().join("fake-archify.mjs");
        std::fs::write(&script, format!("printf '%s' '{DELIVER_OK_JSON}'")).unwrap();
        std::fs::write(tmp.path().join("ir.json"), "{}").unwrap();
        let cfg = cfg_with_fake_node(&script, tmp.path());
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg));
        let out = tools()[2]
            .call(
                serde_json::json!({
                    "type": "architecture", "path": "ir.json", "open": false
                }),
                &ctx,
            )
            .await
            .unwrap();
        assert!(!out.is_error, "{}", out.content);
        // Дефолтный output — <имя-IR>.html в рабочем каталоге, без браузера.
        assert!(out.content.contains("HTML: "), "{}", out.content);
        assert!(out.content.contains("ir.html"), "{}", out.content);
        assert!(out.content.contains("open=false"), "{}", out.content);
    }

    #[tokio::test]
    async fn tool_show_rejects_output_outside_workdir() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("ir.json"), "{}").unwrap();
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(Config::default()));
        let out = tools()[2]
            .call(
                serde_json::json!({
                    "type": "architecture", "path": "ir.json", "output": "../escape.html"
                }),
                &ctx,
            )
            .await
            .unwrap();
        assert!(out.is_error);
        assert!(out.content.contains("за пределами"), "{}", out.content);
    }

    #[tokio::test]
    async fn tool_show_surfaces_cli_failure() {
        let tmp = tempfile::tempdir().unwrap();
        let script = tmp.path().join("fake-archify.mjs");
        std::fs::write(
            &script,
            format!("printf '%s' '{VALIDATE_FAIL_JSON}'; exit 1"),
        )
        .unwrap();
        std::fs::write(tmp.path().join("ir.json"), "{}").unwrap();
        let cfg = cfg_with_fake_node(&script, tmp.path());
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg));
        let out = tools()[2]
            .call(
                serde_json::json!({"type": "architecture", "path": "ir.json", "open": false}),
                &ctx,
            )
            .await
            .unwrap();
        // Провал доставки — ошибка с диагностиками, браузер не открывается.
        assert!(out.is_error, "{}", out.content);
        assert!(!out.content.contains("HTML: "), "{}", out.content);
    }

    #[tokio::test]
    async fn run_times_out_on_hanging_cli() {
        let tmp = tempfile::tempdir().unwrap();
        let script = tmp.path().join("hang.mjs");
        std::fs::write(&script, "sleep 30").unwrap();
        let cfg = cfg_with_fake_node(&script, tmp.path());
        let run = run(&cfg, tmp.path(), &["doctor".to_owned()], 1)
            .await
            .unwrap();
        assert!(run.timed_out, "ожидался таймаут: {run:?}");
        assert!(!run.ok());
        let out = outcome_output("doctor", &run, 1);
        assert!(out.is_error);
        assert!(out.content.contains("таймаут"), "{}", out.content);
    }
}
