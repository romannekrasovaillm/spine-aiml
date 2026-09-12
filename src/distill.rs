//! Дистилляция контекста в архитектурный скилл библиотеки плагинов.
//!
//! КОНТРАКТ (владелец: агент `tools`):
//! - [`distill_to_skill`] — общая процедура: материал → LLM (промпт
//!   `skill_distiller`) → валидный `SKILL.md` → файл
//!   `<plugins_root>/<plugin>/skills/<slug>/SKILL.md`;
//! - зона по умолчанию — плагин `arch-distilled` (managed: там перезапись
//!   разрешена); в чужих плагинах существующий файл не затирается;
//! - инструмент `skill_distill` — дистилляция переданного текста (статьи);
//!   транскрипт текущей сессии дистиллируется слэш-командой `/distill`
//!   (она видит историю, инструмент — нет).

use std::path::{Path, PathBuf};
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::{Value, json};

use crate::config::Config;
use crate::error::{HarnessError, Result};
use crate::llm::{ChatMessage, ChatRequest, LlmProvider, ToolSpec};
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Плагин-зона для дистиллированных скиллов (managed: перезапись разрешена).
pub const DISTILL_PLUGIN_DEFAULT: &str = "arch-distilled";

/// Минимум материала для осмысленной дистилляции (символов).
const MIN_CONTENT_CHARS: usize = 200;
/// Максимум материала, уходящего в промпт (символов).
const MAX_CONTENT_CHARS: usize = 30_000;

/// Маршрут гейта «md vs skill»: что модель решила записать (первая строка
/// ответа, `<!-- routing: … -->`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DistillRoute {
    /// Синтез Agent Skill → `<plugin>/skills/<slug>/SKILL.md`.
    Skill,
    /// Справка или playbook → `<plugin>/distillate/<slug>.md` (площадка
    /// вызревания, не скилл).
    Md,
    /// Процедура, ещё не готовая в скилл → `<plugin>/distillate/playbooks/<slug>.md`
    /// с паспортом (`type: playbook`, журнал `uses`/`steps_sha256`) — площадка
    /// вызревания: после третьего неизменного применения градируется в скилл.
    Playbook,
    /// Материала на артефакт нет — ничего не пишется.
    None,
}

impl DistillRoute {
    /// Строковое имя маршрута (как в маркере) — для вывода инструмента.
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

/// Итог дистилляции.
#[derive(Debug)]
pub struct DistillOutcome {
    /// Путь к записанному файлу (SKILL.md или distillate/<slug>.md).
    pub path: PathBuf,
    /// Финальное имя скилла (slug).
    pub skill_name: String,
    /// Размер записанного файла (символов).
    pub chars: usize,
    /// Маршрут, выбранный моделью (гейт «md vs skill»).
    pub routed: DistillRoute,
}

/// Имя скилла → kebab-case slug: латиница/цифры/дефис, остальное выбрасывается.
/// Пустой результат — признак непригодного имени (ошибка у вызывающей).
#[must_use]
pub fn slugify(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    let mut dash = false;
    for c in name.trim().to_lowercase().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
            dash = false;
        } else if !dash && !out.is_empty() {
            out.push('-');
            dash = true;
        }
    }
    out.trim_end_matches('-').to_string()
}

/// Дистиллирует материал в SKILL.md и записывает в библиотеку плагинов.
///
/// Модель отвечает по промпту `skill_distiller`; если она не вернула
/// frontmatter — он синтезируется (имя = slug, описание — из первой строки).
///
/// # Errors
/// Имя без латиницы/цифр; материала меньше [`MIN_CONTENT_CHARS`]; целевой
/// файл существует вне managed-зоны; ошибка модели или записи файла.
pub async fn distill_to_skill(
    content: &str,
    name: &str,
    plugin: &str,
    provider: &Arc<dyn LlmProvider>,
    plugins_root: &Path,
) -> Result<DistillOutcome> {
    let slug = slugify(name);
    if slug.is_empty() {
        return Err(HarnessError::Tool(format!(
            "имя '{name}' не даёт slug: нужны латинские буквы или цифры"
        )));
    }
    let material: String = content.chars().take(MAX_CONTENT_CHARS).collect();
    if material.chars().count() < MIN_CONTENT_CHARS {
        return Err(HarnessError::Tool(format!(
            "слишком мало материала для дистилляции ({} из минимум {MIN_CONTENT_CHARS} символов)",
            material.chars().count()
        )));
    }
    let plugin = if plugin.trim().is_empty() {
        DISTILL_PLUGIN_DEFAULT
    } else {
        plugin.trim()
    };

    let request = ChatRequest {
        messages: vec![
            ChatMessage::system(crate::assets::PROMPT_SKILL_DISTILLER),
            ChatMessage::user(format!("Имя скилла: {slug}\n\nМатериал:\n{material}")),
        ],
        tools: Vec::new(),
        // temperature не хардкодим: Kimi k3 принимает только 1 (HTTP 400
        // «invalid temperature») — подставится default из ModelConfig
        // (`req.temperature.or(default_temperature)` в openai_compat).
        temperature: None,
        max_tokens: Some(4000),
        thinking: None,
    };
    let raw = provider.complete(request).await?.content;
    // Гейт «md vs skill» (шаг 0 промпта): маршрут — первая строка ответа.
    let route = parse_route(&raw);
    let payload = strip_route_marker(&raw);
    let (target, body) = match route {
        // Материала на артефакт нет — не пишем ничего, возвращаем причину.
        DistillRoute::None => {
            let reason = none_reason(&raw).unwrap_or_else(|| "причина не указана моделью".into());
            return Err(HarnessError::Control(format!(
                "гейт md vs skill: артефакт не создан — {reason}"
            )));
        }
        // Декларация или плейбук вызревания → md в distillate/, контракт
        // скилла не требуется (это не скилл).
        DistillRoute::Md => {
            let target = plugins_root
                .join(plugin)
                .join("distillate")
                .join(format!("{slug}.md"));
            (target, format!("{payload}\n"))
        }
        // Процедура, ещё не доказавшая стабильность → playbook с паспортом:
        // площадка вызревания, контракт скилла пока не требуется.
        DistillRoute::Playbook => {
            let target = plugins_root
                .join(plugin)
                .join("distillate")
                .join("playbooks")
                .join(format!("{slug}.md"));
            let text = stamp_playbook(&payload, &slug);
            (target, text)
        }
        // Скилл: структурный контракт обязателен (триггер + процедура).
        DistillRoute::Skill => {
            let text = ensure_frontmatter(&payload, &slug);
            let violations = crate::control::skill_contract_violations(&text);
            if !violations.is_empty() {
                return Err(HarnessError::Control(format!(
                    "материал не вызрел в скилл: {}",
                    violations.join("; ")
                )));
            }
            let target = plugins_root
                .join(plugin)
                .join("skills")
                .join(&slug)
                .join("SKILL.md");
            (target, text)
        }
    };
    if target.exists() && plugin != DISTILL_PLUGIN_DEFAULT {
        return Err(HarnessError::Tool(format!(
            "{} уже существует вне зоны {DISTILL_PLUGIN_DEFAULT} — не затираю; \
             выберите другое имя или plugin={DISTILL_PLUGIN_DEFAULT}",
            target.display()
        )));
    }

    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
    }
    crate::managed::atomic_write(&target, &body)?;
    Ok(DistillOutcome {
        path: target,
        skill_name: slug,
        chars: body.chars().count(),
        routed: route,
    })
}

/// Маршрут гейта «md vs skill» из первой строки ответа модели
/// (`<!-- routing: skill|md|playbook|none -->`). Отсутствие маркера трактуется как
/// [`DistillRoute::Skill`] — обратная совместимость с моделями, не вернувшими
/// маршрут; контракт скилла всё равно проверяется.
fn parse_route(raw: &str) -> DistillRoute {
    for line in raw.lines().take(5) {
        let Some(inner) = line.trim().strip_prefix("<!--") else {
            continue;
        };
        let inner = inner.strip_suffix("-->").unwrap_or(inner).trim();
        let Some(value) = inner.strip_prefix("routing:") else {
            continue;
        };
        return match value.trim().to_ascii_lowercase().as_str() {
            "md" => DistillRoute::Md,
            "playbook" => DistillRoute::Playbook,
            "none" => DistillRoute::None,
            _ => DistillRoute::Skill,
        };
    }
    DistillRoute::Skill
}

/// Убирает строку-маршрут из ответа модели: в записанном артефакте маркер не
/// хранится — маршрут уже определил путь (skills/ или distillate/).
fn strip_route_marker(raw: &str) -> String {
    let cut = raw.lines().position(is_route_line).map_or(0, |i| i + 1);
    raw.lines()
        .skip(cut)
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string()
}

/// Причина отказа для маршрута `none`: первая непустая строка после маркера.
fn none_reason(raw: &str) -> Option<String> {
    let idx = raw.lines().position(is_route_line)?;
    raw.lines()
        .skip(idx + 1)
        .map(str::trim)
        .find(|l| !l.is_empty())
        .map(str::to_string)
}

/// Строка-маршрут (`<!-- routing: … -->`).
fn is_route_line(line: &str) -> bool {
    let t = line.trim();
    t.starts_with("<!--") && t.contains("routing:") && t.ends_with("-->")
}

/// Гарантирует frontmatter у текста скилла: снимает код-фенсы модели,
/// при отсутствии `---` синтезирует шапку (имя = slug, описание — первая
/// непустая строка тела, усечённая).
fn ensure_frontmatter(raw: &str, slug: &str) -> String {
    let mut text = raw.trim();
    // Модель любит завернуть документ в ```markdown … ``` — снимаем.
    if let Some(rest) = text.strip_prefix("```") {
        let rest = rest.strip_prefix("markdown").unwrap_or(rest);
        let rest = rest.trim_start_matches(['\r', '\n']);
        text = rest.strip_suffix("```").unwrap_or(rest).trim();
    }
    if text.starts_with("---") {
        return format!("{text}\n");
    }
    let first_line = text
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty() && !l.starts_with('#'))
        .unwrap_or("Дистиллированный архитектурный скилл.")
        .trim_start_matches(['*', '-', ' ']);
    let description: String = first_line.chars().take(280).collect();
    format!("---\nname: {slug}\ndescription: {description}\n---\n\n{text}\n")
}

/// Frontmatter-блок текста без обрамляющих `---` (пустая строка, если шапки
/// нет). Границей считается первый `\n---` после открывающего `---`.
fn frontmatter_block(text: &str) -> &str {
    let trimmed = text.trim_start_matches('\u{feff}');
    let Some(after) = trimmed.strip_prefix("---") else {
        return "";
    };
    after
        .split_once("\n---")
        .map_or("", |(fm, _)| fm.trim_start_matches(['\r', '\n']))
}

/// Первая непустая не-заголовочная строка тела — заготовка `description`,
/// если модель описания не написала. Плейбук обязан вырасти в скилл, а контракт
/// скилла требует непустое `description` — без синтеза такой playbook не
/// сградируется. `None`, если тела нет.
fn first_body_line(text: &str) -> Option<String> {
    let body = match text
        .trim_start_matches('\u{feff}')
        .trim_start()
        .strip_prefix("---")
    {
        Some(after) => after.split_once("\n---").map_or(after, |(_, body)| body),
        None => text,
    };
    body.lines()
        .map(str::trim)
        .find(|l| !l.is_empty() && !l.starts_with('#'))
        .map(|l| l.trim_start_matches(['*', '-', ' ']).to_string())
}

/// Раздел «Методика» текста: тело под заголовком-синонимом (методика/метод/
/// процедура/порядок/шаги/how to), обрезанное до следующего заголовка.
/// Нормализацию (CRLF, хвостовые пробелы) и выбор сечения держит
/// [`crate::control::procedure_section`] — тот же раздел читает гейт градации.
/// `None`, если раздела нет.
fn methodology_body(text: &str) -> Option<String> {
    let body = match text
        .trim_start_matches('\u{feff}')
        .trim_start()
        .strip_prefix("---")
    {
        Some(after) => after.split_once("\n---").map_or(after, |(_, body)| body),
        None => text,
    };
    crate::control::procedure_section(body)
}

/// Хэш устойчивости шагов: sha256 нормализованного тела «Методики».
/// `None` — процедуры в тексте нет (и журналу нечего сверять).
fn steps_hash(text: &str) -> Option<String> {
    let body = methodology_body(text)?;
    Some(crate::archunit::sha256_hex(body.as_bytes()))
}

/// Строковый upsert поля frontmatter: заменяет строку `key:` (строгий префикс,
/// как во `frontmatter_field`) или вставляет перед закрывающим `---`.
/// Без шапки — синтезирует её. Тело и порядок прочих полей не трогаются.
fn upsert_fm_field(text: &str, key: &str, value: &str) -> String {
    let trimmed = text.trim_start_matches('\u{feff}');
    let Some(after) = trimmed.strip_prefix("---") else {
        return format!("---\n{key}: {value}\n---\n\n{trimmed}");
    };
    let Some(pos) = after.find("\n---") else {
        return text.to_string();
    };
    let fm = &after[..pos];
    let tail = &after[pos..];
    let mut out = String::new();
    let mut replaced = false;
    for line in fm.split_inclusive('\n') {
        let bare = line.trim_end_matches(['\r', '\n']);
        let hit = bare
            .strip_prefix(key)
            .is_some_and(|rest| rest.starts_with(':'));
        if hit {
            out.push_str(key);
            out.push_str(": ");
            out.push_str(value);
            out.push('\n');
            replaced = true;
        } else {
            out.push_str(line);
        }
    }
    if !replaced {
        if !out.ends_with('\n') {
            out.push('\n');
        }
        out.push_str(key);
        out.push_str(": ");
        out.push_str(value);
        out.push('\n');
    }
    format!("---{out}{tail}")
}

/// Паспорт playbook: гарантирует шапку и проставляет инвариантные поля
/// (§4.1) — `type: playbook`, `status: draft`, `uses: 1`, `skill_candidate`
/// (по умолчанию `true`, явный `false` модели уважается), `steps_sha256`
/// (считает харнесс — не модель), `created` (дата). Тело — процедура — не
/// переписывается: `ensure_frontmatter` только чистит фенсы и синтезирует
/// шапку при её отсутствии.
fn stamp_playbook(raw: &str, slug: &str) -> String {
    let mut text = ensure_frontmatter(raw, slug);
    text = upsert_fm_field(&text, "type", "playbook");
    text = upsert_fm_field(&text, "status", "draft");
    text = upsert_fm_field(&text, "name", slug);
    if crate::control::frontmatter_field(frontmatter_block(&text), "description").is_none() {
        if let Some(line) = first_body_line(&text) {
            let description: String = line.chars().take(280).collect();
            text = upsert_fm_field(&text, "description", &description);
        }
    }
    text = upsert_fm_field(&text, "uses", "1");
    let reject = crate::control::frontmatter_field(frontmatter_block(&text), "skill_candidate")
        .is_some_and(|v| v.trim().eq_ignore_ascii_case("false"));
    if !reject {
        text = upsert_fm_field(&text, "skill_candidate", "true");
    }
    if let Some(hash) = steps_hash(&text) {
        text = upsert_fm_field(&text, "steps_sha256", &hash);
    }
    if crate::control::frontmatter_field(frontmatter_block(&text), "created").is_none() {
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        text = upsert_fm_field(&text, "created", &today);
    }
    if !text.ends_with('\n') {
        text.push('\n');
    }
    text
}

/// Увеличивает журнал применений playbook: `uses` растёт, только если
/// «Методика» не менялась с прошлого применения (сверка `steps_sha256`);
/// изменившаяся процедура обнуляет счётчик — повторения начались заново.
/// Пишет атомарно (tmp + rename). Возвращает новое число применений.
///
/// # Errors
/// Ошибка чтения/записи файла; в тексте нет раздела «Методика» (это не
/// процедура — `HarnessError::Tool`).
pub fn bump_playbook_uses(path: &Path) -> Result<u32> {
    let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let cur = steps_hash(&text).ok_or_else(|| {
        HarnessError::Tool(format!(
            "{}: не процедура — нет раздела «Методика»",
            path.display()
        ))
    })?;
    let fm = frontmatter_block(&text);
    let recorded = crate::control::frontmatter_field(fm, "steps_sha256");
    let old = crate::control::frontmatter_field(fm, "uses")
        .and_then(|v| v.trim().parse::<u32>().ok())
        .unwrap_or(0);
    let uses = if recorded.as_deref().map(str::trim) == Some(cur.as_str()) {
        old.saturating_add(1)
    } else {
        1
    };
    let text = upsert_fm_field(&text, "uses", &uses.to_string());
    let text = upsert_fm_field(&text, "steps_sha256", &cur);
    write_atomic(path, &text)?;
    Ok(uses)
}

/// Атомарная запись: временный файл рядом + rename поверх цели.
fn write_atomic(path: &Path, text: &str) -> Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
    let tmp = path.with_extension("playbook.tmp");
    std::fs::write(&tmp, text).map_err(|e| HarnessError::io(&tmp, e))?;
    std::fs::rename(&tmp, path).map_err(|e| HarnessError::io(path, e))?;
    Ok(())
}

/// Вставляет баннер `> [graduated] → <rel>` первой строкой тела (после
/// frontmatter). Идемпотентно: повторный вызов не дублирует баннер.
fn insert_graduated_banner(text: &str, rel: &str) -> String {
    let Some(after) = text.trim_start_matches('\u{feff}').strip_prefix("---") else {
        return format!("> [graduated] → {rel}\n\n{text}");
    };
    let Some(pos) = after.find("\n---") else {
        return text.to_string();
    };
    let cut = 3 + pos + 4;
    let (head, rest) = text.split_at(cut);
    let body = rest.trim_start_matches(['\r', '\n']);
    if body.trim_start().starts_with("> [graduated]") {
        return text.to_string();
    }
    format!("{head}\n\n> [graduated] → {rel}\n\n{body}")
}

/// Сводка playbook для слэш-команды `/playbook list`: паспорт без чтения тела.
#[derive(Debug, Clone)]
pub(crate) struct PlaybookInfo {
    /// Имя плагина-владельца.
    pub(crate) plugin: String,
    /// Slug playbook (имя файла без расширения).
    pub(crate) slug: String,
    /// Счётчик применений (`uses`); 0, если поля нет.
    pub(crate) uses: u32,
    /// Статус паспорта (`draft`/`stable`/`graduated`).
    pub(crate) status: String,
    /// Кандидат в скиллы (`skill_candidate`; по умолчанию true).
    pub(crate) candidate: bool,
}

/// Перечисляет playbook-кандидаты под `root` — файлы
/// `<plugin>/distillate/playbooks/*.md`, отсортированные по (плагин, slug).
/// Читается только паспорт (`uses`/`status`/`skill_candidate`), не тело.
pub(crate) fn list_playbooks(root: &Path) -> Vec<PlaybookInfo> {
    let mut rows = Vec::new();
    let Ok(entries) = std::fs::read_dir(root) else {
        return rows;
    };
    for entry in entries.flatten() {
        let plugin = entry.file_name().to_string_lossy().to_string();
        let dir = entry.path().join("distillate").join("playbooks");
        let Ok(files) = std::fs::read_dir(&dir) else {
            continue;
        };
        for file in files.flatten() {
            let path = file.path();
            if path.extension().and_then(|e| e.to_str()) != Some("md") {
                continue;
            }
            let Some(slug) = path.file_stem().and_then(|s| s.to_str()) else {
                continue;
            };
            let Ok(text) = std::fs::read_to_string(&path) else {
                continue;
            };
            let fm = frontmatter_block(&text);
            rows.push(PlaybookInfo {
                plugin: plugin.clone(),
                slug: slug.to_string(),
                uses: crate::control::frontmatter_field(fm, "uses")
                    .and_then(|v| v.trim().parse::<u32>().ok())
                    .unwrap_or(0),
                status: crate::control::frontmatter_field(fm, "status")
                    .unwrap_or_else(|| "draft".to_string()),
                candidate: !crate::control::frontmatter_field(fm, "skill_candidate")
                    .is_some_and(|v| v.trim().eq_ignore_ascii_case("false")),
            });
        }
    }
    rows.sort_by(|a, b| {
        (a.plugin.as_str(), a.slug.as_str()).cmp(&(b.plugin.as_str(), b.slug.as_str()))
    });
    rows
}

/// Ищет playbook `<slug>.md` под каталогом плагинов: в заданном плагине либо
/// (plugin пуст) обходом подкаталогов. Возвращает имя плагина и путь.
pub(crate) fn find_playbook(root: &Path, plugin: &str, slug: &str) -> Option<(String, PathBuf)> {
    if !plugin.trim().is_empty() {
        let name = plugin.trim().to_string();
        let path = root
            .join(&name)
            .join("distillate")
            .join("playbooks")
            .join(format!("{slug}.md"));
        return path.is_file().then_some((name, path));
    }
    for entry in std::fs::read_dir(root).ok()?.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        let path = entry
            .path()
            .join("distillate")
            .join("playbooks")
            .join(format!("{slug}.md"));
        if path.is_file() {
            return Some((name, path));
        }
    }
    None
}

/// Градация playbook в Agent Skill (правило трёх повторений).
///
/// Порядок инвариантен: сначала проверки, затем запись скилла, и только после
/// её успеха — архивация playbook (файл не удаляется: он остаётся
/// provenance-историей с пометкой `status: graduated` и `graduated_to`).
/// Повторный вызов отвергается предикатом ([`crate::control::playbook_graduation_violations`]).
///
/// # Errors
/// Нет «Методики»; порог не достигнут (`uses < 3`, нет `steps_sha256`,
/// `skill_candidate: false`); шаги менялись (журнал разошёлся с хэшем);
/// текст не проходит контракт скилла; ошибка записи файла.
pub async fn graduate_playbook(
    playbook_path: &Path,
    plugin: &str,
    plugins_root: &Path,
) -> Result<DistillOutcome> {
    let text =
        std::fs::read_to_string(playbook_path).map_err(|e| HarnessError::io(playbook_path, e))?;
    let Some(hash) = steps_hash(&text) else {
        return Err(HarnessError::Control(
            "не процедура: нет раздела «Методика»".into(),
        ));
    };
    let violations = crate::control::playbook_graduation_violations(&text);
    if !violations.is_empty() {
        return Err(HarnessError::Control(format!(
            "playbook не готов к градации: {}",
            violations.join("; ")
        )));
    }
    let fm = frontmatter_block(&text);
    let recorded = crate::control::frontmatter_field(fm, "steps_sha256").unwrap_or_default();
    if recorded.trim() != hash {
        return Err(HarnessError::Control(
            "шаги менялись — повторений по журналу меньше трёх".into(),
        ));
    }
    let slug = crate::control::frontmatter_field(fm, "name")
        .map(|v| slugify(v.trim()))
        .filter(|s| !s.is_empty())
        .or_else(|| {
            playbook_path
                .file_stem()
                .and_then(|s| s.to_str())
                .map(slugify)
                .filter(|s| !s.is_empty())
        })
        .ok_or_else(|| {
            HarnessError::Tool(format!(
                "{}: не удалось определить slug скилла",
                playbook_path.display()
            ))
        })?;
    let plugin = if plugin.trim().is_empty() {
        DISTILL_PLUGIN_DEFAULT
    } else {
        plugin.trim()
    };

    // Скилл: playbook уже несёт разделы — переопределяем только паспорт.
    let mut skill_text = ensure_frontmatter(&text, &slug);
    skill_text = upsert_fm_field(&skill_text, "type", "skill");
    skill_text = upsert_fm_field(&skill_text, "status", "draft");
    skill_text = upsert_fm_field(&skill_text, "name", &slug);
    let sk_violations = crate::control::skill_contract_violations(&skill_text);
    if !sk_violations.is_empty() {
        return Err(HarnessError::Control(format!(
            "playbook не вызрел в скилл: {}",
            sk_violations.join("; ")
        )));
    }

    let skill_target = plugins_root
        .join(plugin)
        .join("skills")
        .join(&slug)
        .join("SKILL.md");
    if skill_target.exists() && plugin != DISTILL_PLUGIN_DEFAULT {
        return Err(HarnessError::Tool(format!(
            "{} уже существует вне зоны {DISTILL_PLUGIN_DEFAULT} — не затираю",
            skill_target.display()
        )));
    }
    // Скилл пишется ПЕРВЫМ: playbook метится graduated только после успеха.
    write_atomic(&skill_target, &skill_text)?;

    let rel = format!("{plugin}/skills/{slug}/SKILL.md");
    let today = chrono::Local::now().format("%Y-%m-%d").to_string();
    let archived = upsert_fm_field(&text, "status", "graduated");
    let archived = upsert_fm_field(&archived, "graduated_to", &rel);
    let archived = upsert_fm_field(&archived, "graduated_at", &today);
    let archived = insert_graduated_banner(&archived, &rel);
    write_atomic(playbook_path, &archived)?;

    Ok(DistillOutcome {
        path: skill_target,
        skill_name: slug,
        chars: skill_text.chars().count(),
        routed: DistillRoute::Playbook,
    })
}

/// Инструменты домена: `skill_distill`, `playbook_graduate`.
#[must_use]
pub fn tools(cfg: &Config) -> Vec<Arc<dyn Tool>> {
    let root = cfg.plugins.dirs.first().cloned();
    vec![
        Arc::new(SkillDistillTool {
            plugins_root: root.clone(),
        }),
        Arc::new(PlaybookGraduateTool { plugins_root: root }),
    ]
}

/// Инструмент `skill_distill`: дистилляция переданного текста в скилл.
struct SkillDistillTool {
    plugins_root: Option<PathBuf>,
}

#[async_trait]
impl Tool for SkillDistillTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "skill_distill".into(),
            description: "Дистиллировать материал (статью, конспект, заметки) в архитектурный \
                скилл библиотеки: модель выделяет повторяемую методику и пишет SKILL.md в \
                plugins/<plugin>/skills/<name>/. Если процедура ещё не устоялась, модель пишет \
                playbook (площадку вызревания) в plugins/<plugin>/distillate/playbooks/<name>.md \
                с журналом применений; после трёх неизменных применений он градируется в скилл \
                через playbook_graduate. Сначала прочитай источник (read_file/web_fetch), \
                затем передавай текст в content. Транскрипт текущей сессии дистиллируется \
                слэш-командой /distill."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "имя скилла (будет приведено к kebab-case), например adr-staging"
                    },
                    "content": {
                        "type": "string",
                        "description": "текст материала для дистилляции (от 200 символов)"
                    },
                    "plugin": {
                        "type": "string",
                        "description": "целевой плагин (по умолчанию arch-distilled — managed-зона)"
                    }
                },
                "required": ["name", "content"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let name = args
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let content = args.get("content").and_then(Value::as_str).unwrap_or("");
        let plugin = args.get("plugin").and_then(Value::as_str).unwrap_or("");
        let Some(root) = &self.plugins_root else {
            return Ok(ToolOutput::err(
                "не настроены каталоги плагинов ([plugins] dirs) — некуда писать скилл",
            ));
        };
        let Some(provider) = ctx
            .provider
            .clone()
            .or_else(|| ctx.llm.as_ref().map(|r| r.default()))
        else {
            return Ok(ToolOutput::err(
                "дистилляция требует модель: LLM не настроен в контексте",
            ));
        };
        match distill_to_skill(content, name, plugin, &provider, root).await {
            Ok(outcome) => {
                let verb = match outcome.routed {
                    DistillRoute::Md => "сохранено как md (distillate, не скилл)",
                    DistillRoute::Playbook => "сохранено как playbook (площадка вызревания)",
                    DistillRoute::Skill | DistillRoute::None => "дистиллирован",
                };
                let hint = if outcome.routed == DistillRoute::Playbook {
                    format!(
                        " Применений: 1; градация в скилл — playbook_graduate(name=\"{}\") \
                         или /playbook graduate {} (нужно ≥{}).",
                        outcome.skill_name,
                        outcome.skill_name,
                        crate::control::PLAYBOOK_USES_MIN
                    )
                } else {
                    String::new()
                };
                Ok(ToolOutput::ok(format!(
                    "'{}' {verb} → {} ({} символов, routing: {}). \
                     Найдётся через skill_search(\"{}\") или /skills.{hint}",
                    outcome.skill_name,
                    outcome.path.display(),
                    outcome.chars,
                    outcome.routed.as_str(),
                    outcome.skill_name
                )))
            }
            Err(e) => Ok(ToolOutput::err(format!("skill_distill: {e}"))),
        }
    }
}

/// Инструмент `playbook_graduate`: градация вызревшего playbook в скилл.
struct PlaybookGraduateTool {
    plugins_root: Option<PathBuf>,
}

#[async_trait]
impl Tool for PlaybookGraduateTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "playbook_graduate".into(),
            description: "Градировать playbook в Agent Skill по правилу трёх повторений: \
                проверяет готовность (uses ≥ 3, шаги не менялись с последнего применения, \
                есть «Методика», skill_candidate не false), пишет \
                plugins/<plugin>/skills/<slug>/SKILL.md и помечает playbook \
                status: graduated / graduated_to (файл не удаляется — остаётся историей). \
                Отказ при невыполненном пороге."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "slug playbook (имя файла без .md), например adr-staging"
                    },
                    "plugin": {
                        "type": "string",
                        "description": "плагин playbook; без него — поиск по каталогам плагинов"
                    }
                },
                "required": ["name"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let _ = ctx;
        let name = args
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let slug = slugify(name);
        if slug.is_empty() {
            return Ok(ToolOutput::err(
                "playbook_graduate: нужно имя playbook (slug с латиницей или цифрами)",
            ));
        }
        let plugin = args.get("plugin").and_then(Value::as_str).unwrap_or("");
        let Some(root) = &self.plugins_root else {
            return Ok(ToolOutput::err(
                "не настроены каталоги плагинов ([plugins] dirs) — негде искать playbook",
            ));
        };
        let Some((plugin, path)) = find_playbook(root, plugin, &slug) else {
            return Ok(ToolOutput::err(format!(
                "playbook '{slug}' не найден под {}",
                root.display()
            )));
        };
        match graduate_playbook(&path, &plugin, root).await {
            Ok(outcome) => Ok(ToolOutput::ok(format!(
                "playbook '{}' градирован → {} ({} символов). Playbook помечен [graduated] \
                 и оставлен на месте — provenance сохранён.",
                outcome.skill_name,
                outcome.path.display(),
                outcome.chars
            ))),
            Err(e) => Ok(ToolOutput::err(format!("playbook_graduate: {e}"))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Провайдер, возвращающий готовый SKILL.md с frontmatter и маршрутом.
    #[derive(Debug)]
    struct GoodLlm;

    #[async_trait]
    impl LlmProvider for GoodLlm {
        fn name(&self) -> &'static str {
            "good"
        }
        fn model(&self) -> &'static str {
            "good-1"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            Ok(ChatMessage::assistant(
                "<!-- routing: skill -->\n\
                 ---\n\
                 name: saga-staging\n\
                 description: Поэтапное внедрение саги. Используй этот навык при миграции транзакций.\n\
                 ---\n\n\
                 # Saga staging\n\n\
                 ## Когда применять\n\
                 - распределённая транзакция через несколько сервисов\n\n\
                 ## Методика\n\
                 1. Выделить шаги саги и компенсации.\n\
                 2. Описать оркестрацию и идемпотентность.\n\
                 3. Внедрить и проверить в staging.\n",
                Vec::new(),
            ))
        }
    }

    /// Провайдер, возвращающий тело без frontmatter и в код-фенсе.
    #[derive(Debug)]
    struct SloppyLlm;

    #[async_trait]
    impl LlmProvider for SloppyLlm {
        fn name(&self) -> &'static str {
            "sloppy"
        }
        fn model(&self) -> &'static str {
            "sloppy-1"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            Ok(ChatMessage::assistant(
                "```markdown\n# Поэтапное внедрение саги\n\n\
                 Используй этот навык при миграции транзакций.\n\n\
                 ## Когда применять\n- распределённая транзакция\n\n\
                 ## Методика\n1. Шаг.\n2. Шаг.\n3. Шаг.\n```",
                Vec::new(),
            ))
        }
    }

    /// Провайдер-«лекция»: маршрут `skill`, но декларативный md без триггера
    /// и процедуры — гейт обязан отвергнуть.
    #[derive(Debug)]
    struct LectureLlm;

    #[async_trait]
    impl LlmProvider for LectureLlm {
        fn name(&self) -> &'static str {
            "lecture"
        }
        fn model(&self) -> &'static str {
            "lecture-1"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            Ok(ChatMessage::assistant(
                "<!-- routing: skill -->\n\
                 ---\n\
                 name: kv-cache\n\
                 description: Разбор устройства KV-кэша в трансформерах.\n\
                 ---\n\n\
                 # KV-кэш\n\n\
                 KV-кэш хранит ключи и значения прошлых токенов.\n",
                Vec::new(),
            ))
        }
    }

    /// Провайдер, возвращающий md-справку под маршрутом `md`.
    #[derive(Debug)]
    struct MdLlm;

    #[async_trait]
    impl LlmProvider for MdLlm {
        fn name(&self) -> &'static str {
            "md"
        }
        fn model(&self) -> &'static str {
            "md-1"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            Ok(ChatMessage::assistant(
                "<!-- routing: md -->\n\
                 ---\nuses: 2\nskill_candidate: true\n---\n\n\
                 # KV-кэш: справка\n\nKV-кэш хранит ключи и значения прошлых токенов.\n",
                Vec::new(),
            ))
        }
    }

    /// Провайдер, отказывающийся от артефакта (маршрут `none`).
    #[derive(Debug)]
    struct NoneLlm;

    #[async_trait]
    impl LlmProvider for NoneLlm {
        fn name(&self) -> &'static str {
            "none"
        }
        fn model(&self) -> &'static str {
            "none-1"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            Ok(ChatMessage::assistant(
                "<!-- routing: none -->\nматериал декларативный, агент и так справляется — артефакт не нужен",
                Vec::new(),
            ))
        }
    }

    fn long_content() -> String {
        "Материал о внедрении саги в платёжном контуре. ".repeat(20)
    }

    #[test]
    fn slugify_kebab_cases_and_drops_garbage() {
        assert_eq!(slugify("Saga Staging"), "saga-staging");
        assert_eq!(slugify("  adr--authoring  "), "adr-authoring");
        assert_eq!(slugify("nfr_design!"), "nfr-design");
        assert_eq!(slugify("сага"), "", "чистая кириллица не даёт slug");
        assert_eq!(slugify("v2 API-gate"), "v2-api-gate");
    }

    #[tokio::test]
    async fn distill_writes_skill_with_model_frontmatter() {
        let tmp = tempfile::tempdir().expect("tmp");
        let provider: Arc<dyn LlmProvider> = Arc::new(GoodLlm);
        let outcome = distill_to_skill(&long_content(), "Saga Staging!", "", &provider, tmp.path())
            .await
            .expect("distill");
        assert_eq!(outcome.skill_name, "saga-staging");
        let text = std::fs::read_to_string(&outcome.path).expect("read");
        assert!(text.starts_with("---\nname: saga-staging"), "text: {text}");
        assert_eq!(outcome.routed, DistillRoute::Skill);
        assert!(
            outcome
                .path
                .ends_with("arch-distilled/skills/saga-staging/SKILL.md")
        );
    }

    #[tokio::test]
    async fn distill_synthesizes_frontmatter_for_sloppy_model() {
        let tmp = tempfile::tempdir().expect("tmp");
        let provider: Arc<dyn LlmProvider> = Arc::new(SloppyLlm);
        let outcome = distill_to_skill(&long_content(), "saga-staging", "", &provider, tmp.path())
            .await
            .expect("distill");
        let text = std::fs::read_to_string(&outcome.path).expect("read");
        assert!(
            text.starts_with("---\nname: saga-staging\ndescription:"),
            "синтезированный frontmatter: {text}"
        );
        assert!(text.contains("# Поэтапное внедрение саги"));
        assert!(!text.contains("```"), "код-фенс снят: {text}");
    }

    #[tokio::test]
    async fn distill_refuses_tiny_content_and_bad_names() {
        let tmp = tempfile::tempdir().expect("tmp");
        let provider: Arc<dyn LlmProvider> = Arc::new(GoodLlm);
        let err = distill_to_skill("коротко", "ok-name", "", &provider, tmp.path())
            .await
            .expect_err("мало материала");
        assert!(err.to_string().contains("мало материала"), "{err}");
        let err = distill_to_skill(&long_content(), "сага", "", &provider, tmp.path())
            .await
            .expect_err("нет slug");
        assert!(err.to_string().contains("slug"), "{err}");
    }

    #[tokio::test]
    async fn distill_protects_foreign_plugin_but_overwrites_managed_zone() {
        let tmp = tempfile::tempdir().expect("tmp");
        let provider: Arc<dyn LlmProvider> = Arc::new(GoodLlm);
        // Чужой плагин: существующий файл не затираем.
        let foreign = tmp.path().join("arch-core/skills/saga-staging/SKILL.md");
        std::fs::create_dir_all(foreign.parent().expect("parent")).expect("mkdir");
        std::fs::write(&foreign, "пользовательский скилл").expect("write");
        let err = distill_to_skill(
            &long_content(),
            "saga-staging",
            "arch-core",
            &provider,
            tmp.path(),
        )
        .await
        .expect_err("защита чужой зоны");
        assert!(err.to_string().contains("не затираю"), "{err}");
        let kept = std::fs::read_to_string(&foreign).expect("read");
        assert_eq!(kept, "пользовательский скилл");
        // Managed-зона: перезапись разрешена.
        distill_to_skill(&long_content(), "saga-staging", "", &provider, tmp.path())
            .await
            .expect("первый прогон");
        let again = distill_to_skill(&long_content(), "saga-staging", "", &provider, tmp.path())
            .await
            .expect("перезапись в arch-distilled разрешена");
        assert!(again.path.is_file());
    }

    #[tokio::test]
    async fn distill_rejects_non_skill_artifact() {
        let tmp = tempfile::tempdir().expect("tmp");
        let provider: Arc<dyn LlmProvider> = Arc::new(LectureLlm);
        let err = distill_to_skill(&long_content(), "kv-cache", "", &provider, tmp.path())
            .await
            .expect_err("справка под маршрутом skill отвергается");
        assert!(err.to_string().contains("не вызрел"), "{err}");
        assert!(
            !tmp.path().join("arch-distilled/skills/kv-cache").exists(),
            "скилл-конспект не должен писаться"
        );
    }

    #[tokio::test]
    async fn distill_routes_md_to_distillate() {
        let tmp = tempfile::tempdir().expect("tmp");
        let provider: Arc<dyn LlmProvider> = Arc::new(MdLlm);
        let outcome = distill_to_skill(&long_content(), "kv-cache", "", &provider, tmp.path())
            .await
            .expect("md-маршрут");
        assert_eq!(outcome.routed, DistillRoute::Md);
        assert!(
            outcome
                .path
                .ends_with("arch-distilled/distillate/kv-cache.md"),
            "path: {}",
            outcome.path.display()
        );
        let text = std::fs::read_to_string(&outcome.path).expect("read");
        assert!(text.contains("# KV-кэш: справка"), "{text}");
        assert!(
            !text.contains("routing:"),
            "маркер в файле не хранится: {text}"
        );
        assert!(!outcome.path.to_string_lossy().contains("skills"));
    }

    #[tokio::test]
    async fn distill_skips_on_none() {
        let tmp = tempfile::tempdir().expect("tmp");
        let provider: Arc<dyn LlmProvider> = Arc::new(NoneLlm);
        let err = distill_to_skill(&long_content(), "kv-cache", "", &provider, tmp.path())
            .await
            .expect_err("маршрут none — артефакт не создаётся");
        assert!(err.to_string().contains("справляется"), "{err}");
        assert!(!tmp.path().join("arch-distilled").exists());
    }

    // ---- playbook: площадка вызревания процедуры ----

    /// Провайдер, возвращающий playbook: маршрут `playbook`, паспорт черновика.
    #[derive(Debug)]
    struct PlaybookLlm;

    #[async_trait]
    impl LlmProvider for PlaybookLlm {
        fn name(&self) -> &'static str {
            "playbook"
        }
        fn model(&self) -> &'static str {
            "playbook-1"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            Ok(ChatMessage::assistant(
                "<!-- routing: playbook -->\n\
                 ---\n\
                 type: playbook\n\
                 status: draft\n\
                 name: saga-staging\n\
                 description: Используй этот навык при миграции распределённых транзакций.\n\
                 skill_candidate: true\n\
                 ---\n\n\
                 # Внедрение саги в платёжный контур\n\n\
                 ## Когда применять\n\
                 - распределённая транзакция через несколько сервисов\n\n\
                 ## Методика\n\
                 1. Выделить шаги саги и компенсации.\n\
                 2. Описать оркестрацию и идемпотентность.\n\
                 3. Внедрить и проверить в staging.\n",
                Vec::new(),
            ))
        }
    }

    /// Тот же playbook, но модель объявила зрелость (`status: stable`):
    /// харнесс обязан переписать статус в `draft`.
    #[derive(Debug)]
    struct StablePlaybookLlm;

    #[async_trait]
    impl LlmProvider for StablePlaybookLlm {
        fn name(&self) -> &'static str {
            "playbook-stable"
        }
        fn model(&self) -> &'static str {
            "playbook-stable-1"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            Ok(ChatMessage::assistant(
                "<!-- routing: playbook -->\n\
                 ---\n\
                 type: playbook\n\
                 status: stable\n\
                 name: saga-staging\n\
                 description: Используй этот навык при миграции распределённых транзакций.\n\
                 skill_candidate: true\n\
                 uses: 9\n\
                 ---\n\n\
                 # Внедрение саги в платёжный контур\n\n\
                 ## Когда применять\n\
                 - распределённая транзакция через несколько сервисов\n\n\
                 ## Методика\n\
                 1. Выделить шаги саги и компенсации.\n\
                 2. Описать оркестрацию и идемпотентность.\n\
                 3. Внедрить и проверить в staging.\n",
                Vec::new(),
            ))
        }
    }

    /// Тело процедуры: H1 намеренно не синоним («методика»/«процедура»/…),
    /// иначе `skill_contract_violations` с его first-match-семантикой взял бы
    /// заголовок документа вместо раздела шагов.
    fn playbook_body() -> String {
        "# Внедрение саги в платёжный контур\n\n\
         ## Когда применять\n\
         - распределённая транзакция через несколько сервисов\n\n\
         ## Методика\n\
         1. Выделить шаги саги и компенсации.\n\
         2. Описать оркестрацию и идемпотентность.\n\
         3. Внедрить и проверить в staging.\n"
            .to_string()
    }

    /// Ожидаемое тело раздела «Методика» — ровно то, что уходит в `steps_sha256`.
    const METODIKA: &str = "1. Выделить шаги саги и компенсации.\n\
        2. Описать оркестрацию и идемпотентность.\n\
        3. Внедрить и проверить в staging.";

    /// Паспорт зрелого playbook: `uses` и кандидат задаются, хэш шагов —
    /// как в харнессе (или подменённый устаревший журнал).
    fn ripe_playbook(uses: u32, candidate: &str, stale_hash: Option<&str>) -> String {
        let text = format!(
            "---\ntype: playbook\nstatus: draft\nname: saga-staging\n\
             description: Используй этот навык при миграции распределённых транзакций.\n\
             uses: {uses}\nskill_candidate: {candidate}\n---\n\n{}",
            playbook_body()
        );
        let hash = stale_hash.map(str::to_string).or_else(|| steps_hash(&text));
        match hash {
            Some(h) => upsert_fm_field(&text, "steps_sha256", &h),
            None => text,
        }
    }

    /// Кладёт playbook в `root/<plugin>/distillate/playbooks/<slug>.md`.
    fn write_playbook_at(root: &Path, plugin: &str, slug: &str, text: &str) -> PathBuf {
        let path = root
            .join(plugin)
            .join("distillate")
            .join("playbooks")
            .join(format!("{slug}.md"));
        std::fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
        std::fs::write(&path, text).expect("write");
        path
    }

    fn fm_field(path: &Path, key: &str) -> Option<String> {
        let text = std::fs::read_to_string(path).expect("read");
        crate::control::frontmatter_field(frontmatter_block(&text), key)
    }

    async fn distill_playbook(tmp: &Path) -> DistillOutcome {
        let provider: Arc<dyn LlmProvider> = Arc::new(PlaybookLlm);
        distill_to_skill(&long_content(), "saga-staging", "", &provider, tmp)
            .await
            .expect("playbook-маршрут")
    }

    #[tokio::test]
    async fn distill_routes_playbook_to_playbooks_dir() {
        let tmp = tempfile::tempdir().expect("tmp");
        let outcome = distill_playbook(tmp.path()).await;
        assert_eq!(outcome.routed, DistillRoute::Playbook);
        assert_eq!(outcome.routed.as_str(), "playbook");
        assert!(
            outcome
                .path
                .ends_with("arch-distilled/distillate/playbooks/saga-staging.md"),
            "path: {}",
            outcome.path.display()
        );
        assert!(outcome.path.is_file());
        assert!(!outcome.path.to_string_lossy().contains("skills"));
        // Маркер маршрута — служебный: в артефакт не попадает.
        let text = std::fs::read_to_string(&outcome.path).expect("read");
        assert!(!text.contains("routing:"), "маркер не хранится: {text}");
    }

    #[tokio::test]
    async fn distill_playbook_stamps_type_and_uses() {
        let tmp = tempfile::tempdir().expect("tmp");
        let outcome = distill_playbook(tmp.path()).await;
        assert_eq!(fm_field(&outcome.path, "type").as_deref(), Some("playbook"));
        assert_eq!(fm_field(&outcome.path, "uses").as_deref(), Some("1"));
        assert_eq!(
            fm_field(&outcome.path, "skill_candidate").as_deref(),
            Some("true")
        );
        assert_eq!(fm_field(&outcome.path, "status").as_deref(), Some("draft"));
        assert_eq!(
            fm_field(&outcome.path, "name").as_deref(),
            Some("saga-staging")
        );
        assert!(fm_field(&outcome.path, "description").is_some());
    }

    #[tokio::test]
    async fn distill_playbook_stamps_steps_hash() {
        let tmp = tempfile::tempdir().expect("tmp");
        let outcome = distill_playbook(tmp.path()).await;
        let hash = fm_field(&outcome.path, "steps_sha256").expect("журнал шагов");
        assert_eq!(hash.len(), 64, "sha256 в hex: {hash}");
        assert_eq!(hash, crate::archunit::sha256_hex(METODIKA.as_bytes()));
    }

    #[tokio::test]
    async fn distill_playbook_forces_draft() {
        let tmp = tempfile::tempdir().expect("tmp");
        let provider: Arc<dyn LlmProvider> = Arc::new(StablePlaybookLlm);
        let outcome = distill_to_skill(&long_content(), "saga-staging", "", &provider, tmp.path())
            .await
            .expect("playbook");
        assert_eq!(fm_field(&outcome.path, "status").as_deref(), Some("draft"));
        assert_eq!(
            fm_field(&outcome.path, "uses").as_deref(),
            Some("1"),
            "счётчик применений ведёт харнесс, не модель"
        );
    }

    #[tokio::test]
    async fn bump_playbook_uses_increments_same_steps() {
        let tmp = tempfile::tempdir().expect("tmp");
        let outcome = distill_playbook(tmp.path()).await;
        assert_eq!(bump_playbook_uses(&outcome.path).expect("bump 1"), 2);
        assert_eq!(bump_playbook_uses(&outcome.path).expect("bump 2"), 3);
        assert_eq!(fm_field(&outcome.path, "uses").as_deref(), Some("3"));
        assert_eq!(
            fm_field(&outcome.path, "steps_sha256").as_deref(),
            steps_hash(&std::fs::read_to_string(&outcome.path).expect("read")).as_deref()
        );
    }

    #[tokio::test]
    async fn bump_playbook_uses_resets_on_step_change() {
        let tmp = tempfile::tempdir().expect("tmp");
        let outcome = distill_playbook(tmp.path()).await;
        assert_eq!(bump_playbook_uses(&outcome.path).expect("bump 1"), 2);
        // Правим именно шаги — журнал обязан обнулиться до 1.
        let text = std::fs::read_to_string(&outcome.path).expect("read");
        std::fs::write(
            &outcome.path,
            format!("{text}\n4. Задокументировать компенсации.\n"),
        )
        .expect("edit");
        assert_eq!(
            bump_playbook_uses(&outcome.path).expect("bump после правки"),
            1
        );
        assert_eq!(fm_field(&outcome.path, "uses").as_deref(), Some("1"));
        assert_eq!(
            fm_field(&outcome.path, "steps_sha256").as_deref(),
            steps_hash(&std::fs::read_to_string(&outcome.path).expect("read")).as_deref(),
            "хэш обновлён под новую процедуру"
        );
    }

    #[tokio::test]
    async fn bump_playbook_uses_errors_without_metodika() {
        let tmp = tempfile::tempdir().expect("tmp");
        let path = tmp.path().join("playbook.md");
        let text = "---\ntype: playbook\nuses: 2\n---\n\n# Заметка\n\nДекларация без процедуры.\n";
        std::fs::write(&path, text).expect("write");
        let err = bump_playbook_uses(&path).expect_err("раздела методики нет");
        assert!(matches!(err, HarnessError::Tool(_)), "{err}");
        assert!(err.to_string().contains("Методика"), "{err}");
        assert_eq!(
            std::fs::read_to_string(&path).expect("read"),
            text,
            "файл не тронут"
        );
    }

    #[tokio::test]
    async fn graduate_refuses_below_three_uses() {
        let tmp = tempfile::tempdir().expect("tmp");
        let path = write_playbook_at(
            tmp.path(),
            "arch-distilled",
            "saga-staging",
            &ripe_playbook(2, "true", None),
        );
        let err = graduate_playbook(&path, "arch-distilled", tmp.path())
            .await
            .expect_err("два применения — рано");
        assert!(matches!(err, HarnessError::Control(_)), "{err}");
        assert!(err.to_string().contains("uses = 2"), "{err}");
        assert!(!tmp.path().join("arch-distilled/skills").exists());
    }

    #[tokio::test]
    async fn graduate_refuses_missing_candidate() {
        let tmp = tempfile::tempdir().expect("tmp");
        let path = write_playbook_at(
            tmp.path(),
            "arch-distilled",
            "saga-staging",
            &ripe_playbook(3, "false", None),
        );
        let err = graduate_playbook(&path, "arch-distilled", tmp.path())
            .await
            .expect_err("не кандидат");
        assert!(matches!(err, HarnessError::Control(_)), "{err}");
        assert!(err.to_string().contains("skill_candidate"), "{err}");
        assert!(!tmp.path().join("arch-distilled/skills").exists());
    }

    #[tokio::test]
    async fn graduate_refuses_changed_steps() {
        let tmp = tempfile::tempdir().expect("tmp");
        let stale = "0".repeat(64);
        let path = write_playbook_at(
            tmp.path(),
            "arch-distilled",
            "saga-staging",
            &ripe_playbook(3, "true", Some(&stale)),
        );
        let err = graduate_playbook(&path, "arch-distilled", tmp.path())
            .await
            .expect_err("журнал не сходится с шагами");
        assert!(matches!(err, HarnessError::Control(_)), "{err}");
        assert!(err.to_string().contains("шаги менялись"), "{err}");
        assert!(!tmp.path().join("arch-distilled/skills").exists());
    }

    #[tokio::test]
    async fn graduate_writes_skill_and_marks_graduated() {
        let tmp = tempfile::tempdir().expect("tmp");
        let path = write_playbook_at(
            tmp.path(),
            "arch-distilled",
            "saga-staging",
            &ripe_playbook(3, "true", None),
        );
        let outcome = graduate_playbook(&path, "arch-distilled", tmp.path())
            .await
            .expect("градация");
        assert_eq!(outcome.skill_name, "saga-staging");
        assert_eq!(outcome.routed, DistillRoute::Playbook);
        let skill = tmp
            .path()
            .join("arch-distilled/skills/saga-staging/SKILL.md");
        assert_eq!(outcome.path, skill);
        assert!(skill.is_file());
        let skill_text = std::fs::read_to_string(&skill).expect("read skill");
        assert!(skill_text.contains("## Методика"), "{skill_text}");
        assert_eq!(
            crate::control::frontmatter_field(frontmatter_block(&skill_text), "type").as_deref(),
            Some("skill"),
            "плейбук перештампован в скилл"
        );
        // Плейбук не удаляется: provenance и журнал применений остаются.
        assert!(path.is_file(), "playbook оставлен на месте");
        assert_eq!(fm_field(&path, "status").as_deref(), Some("graduated"));
        assert_eq!(
            fm_field(&path, "graduated_to").as_deref(),
            Some("arch-distilled/skills/saga-staging/SKILL.md")
        );
        assert!(fm_field(&path, "graduated_at").is_some());
        let text = std::fs::read_to_string(&path).expect("read playbook");
        let body = text.split_once("\n---").map_or("", |(_, b)| b);
        assert!(
            body.trim_start()
                .starts_with("> [graduated] → arch-distilled/skills/saga-staging/SKILL.md"),
            "баннер градации: {body}"
        );
    }

    #[tokio::test]
    async fn graduate_is_not_idempotent_silently() {
        let tmp = tempfile::tempdir().expect("tmp");
        let path = write_playbook_at(
            tmp.path(),
            "arch-distilled",
            "saga-staging",
            &ripe_playbook(3, "true", None),
        );
        graduate_playbook(&path, "arch-distilled", tmp.path())
            .await
            .expect("первая градация");
        let skill = tmp
            .path()
            .join("arch-distilled/skills/saga-staging/SKILL.md");
        std::fs::write(&skill, "маркер: не перезаписывать").expect("marker");
        let err = graduate_playbook(&path, "arch-distilled", tmp.path())
            .await
            .expect_err("повторная градация не молчит");
        assert!(matches!(err, HarnessError::Control(_)), "{err}");
        assert!(err.to_string().contains("уже градирован"), "{err}");
        assert_eq!(
            std::fs::read_to_string(&skill).expect("read"),
            "маркер: не перезаписывать",
            "скилл не перезаписан"
        );
    }

    #[tokio::test]
    async fn graduate_protects_foreign_plugin() {
        let tmp = tempfile::tempdir().expect("tmp");
        let path = write_playbook_at(
            tmp.path(),
            "arch-core",
            "saga-staging",
            &ripe_playbook(3, "true", None),
        );
        let foreign = tmp.path().join("arch-core/skills/saga-staging/SKILL.md");
        std::fs::create_dir_all(foreign.parent().expect("parent")).expect("mkdir");
        std::fs::write(&foreign, "пользовательский скилл").expect("write");
        let err = graduate_playbook(&path, "arch-core", tmp.path())
            .await
            .expect_err("чужая зона");
        assert!(matches!(err, HarnessError::Tool(_)), "{err}");
        assert!(err.to_string().contains("не затираю"), "{err}");
        assert_eq!(
            std::fs::read_to_string(&foreign).expect("read"),
            "пользовательский скилл"
        );
        assert!(
            fm_field(&path, "graduated_to").is_none(),
            "при отказе playbook не помечается"
        );
    }

    #[tokio::test]
    async fn graduate_rejects_non_skill_shape() {
        let tmp = tempfile::tempdir().expect("tmp");
        let thin = "---\ntype: playbook\nstatus: draft\nname: thin-proc\n\
                    description: Используй этот навык при проверке выкладки.\n\
                    uses: 3\nskill_candidate: true\n---\n\n\
                    # Тонкая процедура\n\n## Методика\n1. Единственный шаг.\n"
            .to_string();
        let text = upsert_fm_field(&thin, "steps_sha256", &steps_hash(&thin).expect("методика"));
        let path = write_playbook_at(tmp.path(), "arch-distilled", "thin-proc", &text);
        let err = graduate_playbook(&path, "arch-distilled", tmp.path())
            .await
            .expect_err("одного шага мало");
        assert!(matches!(err, HarnessError::Control(_)), "{err}");
        assert!(err.to_string().contains("менее 3 шагов"), "{err}");
        assert!(!tmp.path().join("arch-distilled/skills").exists());
    }

    #[test]
    fn parse_route_playbook_marker() {
        assert_eq!(
            parse_route("<!-- routing: playbook -->\n# Процедура"),
            DistillRoute::Playbook
        );
        assert_eq!(
            parse_route("<!-- routing: PLAYBOOK -->\n# Процедура"),
            DistillRoute::Playbook
        );
        assert_eq!(
            parse_route("<!-- routing: md -->\n# Справка"),
            DistillRoute::Md
        );
        assert_eq!(
            parse_route("<!-- routing: skill -->\n# Скилл"),
            DistillRoute::Skill
        );
        assert_eq!(parse_route("без маркера"), DistillRoute::Skill);
    }
}
