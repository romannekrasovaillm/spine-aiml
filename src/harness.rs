//! Адаптеры кодовых харнессов и handoff-пакеты (передача архитектуры в код).
//!
//! КОНТРАКТ (владелец: агент `harness`):
//! - известные харнессы: claude-code, qwen-code, openclaw, hermes, theseus,
//!   codewhale, kimi-code ([`known`]); конфиги — из `Config::harnesses`;
//! - [`generate_handoff`] — каталог `<repo>/.arch-handoff/`: TASK.md (задача +
//!   критерии приёмки из QAS-сущностей `<repo>/model/`, ADR-007),
//!   ARCHITECTURE.md (свод спек/спайна), adr/ (копии ADR), CONSTRAINTS.yaml
//!   (fitness-правила под стек репозитория — заготовка, переписывается
//!   архитектором под spine), SPEC.md (шаблон верифицируемых контрактов
//!   интерфейсов: входы/выходы, структуры данных, границы ошибок, критерии
//!   верификации), RUBRIC.yaml (якорная рубрика приёмки), ROLLBACK.yaml
//!   (машиночитаемый план отката — репетируется на гейте A4, см.
//!   `crate::rehearsal`), MANIFEST.json
//!   (мета: дата, модель, источники, маршрут, `baseline_commit`, `rollback_plan`) +
//!   компактный epic-context (800–1500
//!   токенов, по смыслу);
//! - [`run_harness`] — запуск бинаря харнесса (`PromptMode` positional/flag/stdin)
//!   в каталоге repo, таймаут, захват stdout/stderr → `HarnessRun`;
//! - [`tools`] — инструменты `handoff_create` и `harness_run` для агентного
//!   цикла (прогон пакета харнессом — только через `harness_run`, не bash);
//!   `harness_run(background=true)` — фоновый прогон: инструмент возвращается
//!   сразу (задача `hr-*` в общем реестре фоновых задач), агент остаётся
//!   доступным пользователю, результат — через `subagent_result`.

use std::fmt::Write as _;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

use crate::config::{CodingHarnessConfig, Config, PromptMode};
use crate::control::Route;
use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::model::{EntityKind, load_model};
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Имя каталога handoff-пакета в корне репозитория.
const HANDOFF_DIR: &str = ".arch-handoff";

/// Минимальный абсолютный таймаут прогона кодового харнесса, секунд:
/// меньшие значения (модель оптимистично просит «5 минут») поднимаются —
/// ранний обрыв оставлял репозиторий в полусобранном состоянии.
const MIN_HARNESS_TIMEOUT_SECS: u64 = 600;

/// Максимальный размер epic-context (ARCHITECTURE.md), символов
/// (~1500 токенов при грубой оценке 4 символа ≈ 1 токен).
const EPIC_CONTEXT_MAX_CHARS: usize = 6000;

/// Целевой минимум epic-context, символов (~800 токенов — низ окна рубрики
/// `handoff_quality`). Если на глубине «2 абзаца на секцию» контекст меньше,
/// секции перерендериваются глубже ([`DEPTH_DEEP`]).
const EPIC_CONTEXT_MIN_CHARS: usize = 3200;

/// Глубина рендера прочих секций по умолчанию (абзацев на секцию).
const DEPTH_SHALLOW: usize = 2;
/// Глубина рендера прочих секций при недоборе epic-context (абзацев).
const DEPTH_DEEP: usize = 8;

/// Шаблон SPEC.md — верифицируемые контракты интерфейсов компонента
/// (модель «5.2»: прозаический ARCHITECTURE.md компонента заменяется spec'ом
/// с контрактами, проверяемыми тестами). Пишется только при отсутствии —
/// заполненный архитектором файл повторная генерация не затирает.
const SPEC_TEMPLATE: &str = "# SPEC — контракты интерфейсов компонента\n\
\n\
> Шаблон handoff-пакета (НЕ затирается при повторной генерации). Заполняется\n\
> архитектором ДО передачи: верифицируемые контракты вместо прозы. Требования\n\
> — в духе EARS: When <событие>, the <система> shall <реакция>.\n\
\n\
## Входы (контракты соседей)\n\
\n\
- <что компонент потребляет: API/события/файлы, от кого, формат и инварианты>\n\
\n\
## Выходы (публикуемые контракты)\n\
\n\
- <что компонент публикует: API/события/модели данных, гарантии (идемпотентность, порядок, версии)>\n\
\n\
## Структуры данных\n\
\n\
- <ключевые типы/схемы на границах: поля, единицы, ограничения>\n\
\n\
## Границы ошибок\n\
\n\
- <какие ошибки возвращаются/маппятся, какие эскалируются; коды и семантика повторов>\n\
\n\
## Критерии верификации (тесты)\n\
\n\
- [ ] When <событие>, the <система> shall <реакция> — <каким тестом проверяется>\n\
";

/// Собирает SPEC.md из переданных спек архитектора: полные тексты контрактов
/// с заголовками-источниками (шаблон-заполнитель уже не нужен — контракты
/// переданы явно). Не-UTF8 читается с потерями.
fn render_spec_from_sources(spec_files: &[PathBuf]) -> String {
    let mut out = String::from(
        "# SPEC — контракты интерфейсов компонента\n\n\
         > Собрано автоматически из спек, переданных архитектором (`--spec`),\n\
         > при генерации пакета. НЕ затирается при повторной генерации —\n\
         > правьте под фактические контракты эпика.\n",
    );
    for path in spec_files {
        // Файл уже читался компилятором epic-context — падение маловероятно;
        // lossy-фолбэк вместо второй ошибки чтения.
        let text = std::fs::read(path)
            .map(|b| String::from_utf8_lossy(&b).into_owned())
            .unwrap_or_default();
        let _ = write!(
            out,
            "\n\n---\n\n## Источник: {}\n\n{}\n",
            path.display(),
            text.trim()
        );
    }
    out
}

/// Дефолтные fitness-правила под стек репозитория (по маркерным файлам):
/// Cargo.toml → Rust; pyproject.toml/requirements.txt/setup.py → Python;
/// go.mod → Go; package.json → Node; иначе — минимальный общий набор.
/// Пишутся только при отсутствии пользовательского CONSTRAINTS.yaml и всегда
/// остаются заготовкой: перед передачей архитектор переписывает их под
/// spine-инварианты (AD-n) эпика.
fn default_constraints(repo: &Path) -> String {
    let stack = if repo.join("Cargo.toml").is_file() {
        "Rust"
    } else if ["pyproject.toml", "requirements.txt", "setup.py"]
        .iter()
        .any(|m| repo.join(m).is_file())
    {
        "Python"
    } else if repo.join("go.mod").is_file() {
        "Go"
    } else if repo.join("package.json").is_file() {
        "Node"
    } else {
        "generic"
    };
    let rules = match stack {
        "Rust" => {
            "\
  - name: no-unwrap-in-src
    type: must_not_contain
    glob: \"src/**\"
    pattern: 'unwrap\\('
    severity: warn
  - name: no-dbg-macro
    type: must_not_contain
    glob: \"src/**\"
    pattern: 'dbg!'
    severity: error
  - name: readme-exists
    type: file_exists
    path: README.md
    severity: warn
  - name: cargo-check-passes
    type: command_succeeds
    command: 'cargo check'
    timeout_secs: 120
    severity: error
"
        }
        "Python" => {
            "\
  - name: no-print-in-py
    type: must_not_contain
    glob: \"**/*.py\"
    pattern: 'print\\('
    severity: warn
  - name: readme-exists
    type: file_exists
    path: README.md
    severity: warn
  - name: pytest-passes
    type: command_succeeds
    command: 'pytest -q'
    timeout_secs: 180
    severity: error
"
        }
        "Go" => {
            "\
  - name: go-build-passes
    type: command_succeeds
    command: 'go build ./...'
    timeout_secs: 180
    severity: error
  - name: go-vet-passes
    type: command_succeeds
    command: 'go vet ./...'
    timeout_secs: 180
    severity: warn
  - name: readme-exists
    type: file_exists
    path: README.md
    severity: warn
"
        }
        "Node" => {
            "\
  - name: readme-exists
    type: file_exists
    path: README.md
    severity: warn
  - name: npm-test-passes
    type: command_succeeds
    command: 'npm test'
    timeout_secs: 300
    severity: warn
"
        }
        _ => {
            "\
  - name: readme-exists
    type: file_exists
    path: README.md
    severity: warn
"
        }
    };
    format!(
        "# Fitness-правила для `arch-ml control check` (схема control::check).\n\
         # Стек: {stack} (детектирован по маркерным файлам). Заготовка генератора\n\
         # handoff (файл НЕ затирается при повторной генерации): перед передачей\n\
         # перепишите правила под spine-инварианты (AD-n) эпика.\n\
         rules:\n{rules}"
    )
}

/// Имена известных кодовых харнессов.
#[must_use]
pub fn known() -> Vec<&'static str> {
    vec![
        "claude-code",
        "qwen-code",
        "openclaw",
        "hermes",
        "theseus",
        "codewhale",
        "kimi-code",
    ]
}

/// Итог генерации handoff-пакета.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HandoffPacket {
    /// Каталог `.arch-handoff/`.
    pub dir: PathBuf,
    /// Файлы пакета (включая сохранённые пользовательские CONSTRAINTS.yaml/SPEC.md/RUBRIC.yaml).
    pub files: Vec<PathBuf>,
    /// Оценка размера epic-context в токенах.
    pub epic_context_tokens: usize,
    /// Baseline-коммит (якорь отката) на момент генерации пакета.
    #[serde(default)]
    pub baseline: Option<String>,
    /// Git-репозиторий был инициализирован предгейтом (`git init`).
    #[serde(default)]
    pub git_initialized: bool,
    /// На момент генерации есть незакоммиченные изменения отслеживаемых
    /// файлов (откат на baseline их потеряет).
    #[serde(default)]
    pub git_dirty_tracked: bool,
    /// Рекомендованный таймаут прогона по маршруту значимости, секунд.
    #[serde(default)]
    pub recommended_timeout_secs: u64,
}

/// Метаданные пакета (`MANIFEST.json`).
#[derive(Serialize)]
struct Manifest<'a> {
    /// Дата создания, ISO 8601 (UTC).
    created_at: String,
    /// Формулировка задачи.
    task: &'a str,
    /// Модель по умолчанию из конфига.
    model: &'a str,
    /// Файлы-источники спецификаций.
    sources: Vec<String>,
    /// Размер epic-context, символов.
    epic_context_chars: usize,
    /// Оценка размера epic-context, токенов (~chars/4).
    epic_context_tokens: usize,
    /// Маршрут значимости (Fast/Standard/Critical).
    route: String,
    /// Рекомендованный таймаут прогона по маршруту, секунд.
    recommended_timeout_secs: u64,
    /// Baseline-коммит (якорь отката) на момент генерации пакета.
    #[serde(skip_serializing_if = "Option::is_none")]
    baseline_commit: Option<String>,
    /// План отката (текст раздела «План отката» TASK.md).
    rollback_plan: &'a str,
}

/// Генерирует handoff-пакет в репозиторий.
///
/// Создаёт `<repo>/.arch-handoff/` с TASK.md, ARCHITECTURE.md, MANIFEST.json,
/// adr/ (копии ADR) и, при отсутствии, CONSTRAINTS.yaml, SPEC.md и RUBRIC.yaml.
/// Перезаписываются только TASK.md, ARCHITECTURE.md и MANIFEST.json —
/// пользовательские правки CONSTRAINTS.yaml/SPEC.md/RUBRIC.yaml сохраняются.
///
/// Предгейт: гарантирует git-репозиторий и baseline-коммит-якорь отката
/// ([`ensure_git_baseline`]); `rollback` — явный план отката в TASK.md
/// (по умолчанию — откат на baseline с сигналами и владельцем решения);
/// `route` задаёт рекомендованный таймаут прогона (MANIFEST.json подхватывает
/// `harness_run`, когда `timeout_secs` не задан явно).
///
/// Маршрут Critical (rollback-first): пакет не собирается без git-якоря
/// отката (`baseline_commit`) и непустого плана отката — репетиция на гейте A4
/// (`crate::rehearsal`) требует обоих.
///
/// # Errors
/// Репозиторий недоступен, спека не читается, ошибка записи.
pub fn generate_handoff(
    repo: &Path,
    task: &str,
    spec_files: &[PathBuf],
    cfg: &Config,
    rollback: Option<&str>,
    route: Route,
) -> Result<HandoffPacket> {
    if !repo.is_dir() {
        return Err(HarnessError::Harness(format!(
            "репозиторий недоступен: {}",
            repo.display()
        )));
    }
    let baseline = ensure_git_baseline(repo);
    let rollback_text =
        rollback.map_or_else(|| default_rollback(baseline.hash.as_deref()), str::to_owned);
    // Rollback-first для маршрута Critical: пакет обязан нести якорь отката
    // (baseline_commit) и непустой план отката — без них репетиция на гейте
    // A4 невозможна, а откат превращается в импровизацию на инциденте.
    if route == Route::Critical {
        if baseline.hash.is_none() {
            return Err(HarnessError::Harness(
                "маршрут Critical требует baseline_commit (git-якорь отката), но git \
                 в репозитории недоступен — пакет не собирается"
                    .into(),
            ));
        }
        if rollback_text.trim().is_empty() {
            return Err(HarnessError::Harness(
                "маршрут Critical требует непустой план отката (rollback_plan): передайте \
                 --rollback с шагами отката или опустите его — дефолт откатит на baseline"
                    .into(),
            ));
        }
    }
    let timeout = recommended_timeout(route);
    let dir = repo.join(HANDOFF_DIR);
    let adr_dir = dir.join("adr");
    std::fs::create_dir_all(&adr_dir).map_err(|e| HarnessError::io(&adr_dir, e))?;

    // TASK.md — всегда перезаписывается (задача новая на каждый прогон).
    // Критерии приёмки разворачиваются из QAS-сущностей модели репозитория
    // (ADR-007): нет model/ или нет QAS — секции нет; битая модель — ошибка.
    let qas_section = qas_acceptance_section(repo)?;
    let task_path = dir.join("TASK.md");
    std::fs::write(
        &task_path,
        render_task_md(task, &rollback_text, qas_section.as_deref()),
    )
    .map_err(|e| HarnessError::io(&task_path, e))?;

    // ARCHITECTURE.md — всегда перезаписывается (компиляция актуальных спек).
    let arch_md = compile_epic_context(spec_files)?;
    let arch_path = dir.join("ARCHITECTURE.md");
    std::fs::write(&arch_path, &arch_md).map_err(|e| HarnessError::io(&arch_path, e))?;
    let epic_chars = arch_md.chars().count();
    let epic_tokens = epic_chars / 4;

    // Маршрут Critical требует полного epic-context: ниже окна рубрики
    // (800 токенов) пакет не собирается — «реализация без доступа к
    // источникам» на пустом контексте означает архитектурные изобретения
    // исполнителя (разрыв P2: Fast-окно молча прошло бы и для Critical).
    if route == Route::Critical && epic_tokens < EPIC_CONTEXT_MIN_CHARS / 4 {
        return Err(HarnessError::Harness(format!(
            "epic-context ~{epic_tokens} токенов — ниже окна рубрики handoff_quality \
             ({}); для маршрута Critical пакет не собирается: передайте спеки через \
             `spec`/`--spec` (spine с AD-инвариантами, затронутые ADR, NFR) или \
             понизьте маршрут осознанно",
            EPIC_CONTEXT_MIN_CHARS / 4
        )));
    }

    // CONSTRAINTS.yaml — только при отсутствии (не затирать пользовательские правила).
    // Дефолт — под стек репозитория (Cargo.toml/pyproject.toml/go.mod/package.json).
    let constraints_path = dir.join("CONSTRAINTS.yaml");
    if !constraints_path.exists() {
        std::fs::write(&constraints_path, default_constraints(repo))
            .map_err(|e| HarnessError::io(&constraints_path, e))?;
    }

    // SPEC.md — только при отсутствии (не затирать правки архитектора):
    // с переданными спеками — собирается ИЗ НИХ (полные тексты контрактов,
    // кейс 2026-09-01: исполнители спотыкались о пустой шаблон, когда
    // архитектор передал спеки через --spec); без спек — шаблон.
    let spec_path = dir.join("SPEC.md");
    if !spec_path.exists() {
        let spec_md = if spec_files.is_empty() {
            SPEC_TEMPLATE.to_string()
        } else {
            render_spec_from_sources(spec_files)
        };
        std::fs::write(&spec_path, spec_md).map_err(|e| HarnessError::io(&spec_path, e))?;
    }

    // ROLLBACK.yaml — машиночитаемый план отката для репетиции на гейте A4;
    // только при отсутствии: архитектор переписывает шаги под фактический
    // план эпика, повторная генерация его правки не затирает.
    let rollback_path = dir.join(crate::rehearsal::ROLLBACK_FILE);
    if !rollback_path.exists() {
        std::fs::write(
            &rollback_path,
            render_rollback_yaml(baseline.hash.as_deref()),
        )
        .map_err(|e| HarnessError::io(&rollback_path, e))?;
    }

    // RUBRIC.yaml — только при отсутствии и только если есть якорная рубрика.
    let rubric_path = dir.join("RUBRIC.yaml");
    if !rubric_path.exists() {
        let anchor = cfg.paths.rubrics_dir().join("handoff_quality.yaml");
        if anchor.is_file() {
            std::fs::copy(&anchor, &rubric_path).map_err(|e| HarnessError::io(&rubric_path, e))?;
        }
    }

    // adr/ — копии ADR-файлов; существующие копии не затираем.
    let mut adr_copies = Vec::new();
    for spec in spec_files {
        if is_adr_file(spec) {
            let Some(name) = spec.file_name() else {
                continue;
            };
            let dest = adr_dir.join(name);
            if !dest.exists() {
                std::fs::copy(spec, &dest).map_err(|e| HarnessError::io(&dest, e))?;
            }
            adr_copies.push(dest);
        }
    }

    // MANIFEST.json — всегда перезаписывается.
    let manifest = Manifest {
        created_at: Utc::now().to_rfc3339(),
        task,
        model: &cfg.default_model,
        sources: spec_files.iter().map(|p| p.display().to_string()).collect(),
        epic_context_chars: epic_chars,
        epic_context_tokens: epic_tokens,
        route: route.to_string(),
        recommended_timeout_secs: timeout,
        baseline_commit: baseline.hash.clone(),
        rollback_plan: &rollback_text,
    };
    let manifest_path = dir.join("MANIFEST.json");
    let manifest_text = serde_json::to_string_pretty(&manifest)?;
    std::fs::write(&manifest_path, format!("{manifest_text}\n"))
        .map_err(|e| HarnessError::io(&manifest_path, e))?;

    let mut files = vec![task_path, arch_path, manifest_path];
    if constraints_path.exists() {
        files.push(constraints_path);
    }
    if spec_path.exists() {
        files.push(spec_path);
    }
    if rollback_path.exists() {
        files.push(rollback_path);
    }
    if rubric_path.exists() {
        files.push(rubric_path);
    }
    files.extend(adr_copies);

    Ok(HandoffPacket {
        dir,
        files,
        epic_context_tokens: epic_tokens,
        baseline: baseline.hash,
        git_initialized: baseline.initialized,
        git_dirty_tracked: baseline.dirty_tracked,
        recommended_timeout_secs: timeout,
    })
}

/// Рендерит TASK.md: задача + критерии приёмки из QAS (при наличии) +
/// план отката + финализация (git-коммит) + контракт результата
/// (headless JSON-статус).
fn render_task_md(task: &str, rollback: &str, acceptance: Option<&str>) -> String {
    let mut s = String::with_capacity(task.len() + rollback.len() + 2000);
    s.push_str("# Задача для кодового харнесса\n\n");
    s.push_str(task.trim());
    s.push('\n');
    if let Some(acceptance) = acceptance {
        s.push('\n');
        s.push_str(acceptance.trim());
        s.push('\n');
    }
    s.push_str("\n## План отката\n\n");
    s.push_str(rollback.trim());
    s.push('\n');
    s.push_str("\n## Финализация (обязательно)\n\n");
    s.push_str("Результат забирается из git, поэтому перед финальным ответом зафиксируй работу коммитом:\n\n");
    s.push_str("```bash\ngit add -A -- . ':!.arch-handoff'\ngit commit -m \"<кратко: что реализовано>\"\ngit status --short   # пусто, кроме .arch-handoff/\n```\n\n");
    s.push_str(
        "- Коммитится код и тесты; служебный каталог `.arch-handoff/` в коммит не входит.\n",
    );
    s.push_str("- Работа без коммита считается невыполненной: оркестратор увидит её только через git log.\n");
    s.push_str("\n## Контракт результата\n\n");
    s.push_str("Финальный ответ обязан завершаться JSON-объектом (после него — ни символа):\n\n");
    s.push_str("```json\n{\"status\": \"complete|partial|blocked\", \"assumptions\": [], \"open_questions\": [], \"conflicts_with_prior_decisions\": []}\n```\n\n");
    s.push_str("- `status`: `complete` — выполнено полностью; `partial` — частично; `blocked` — заблокировано.\n");
    s.push_str("- `assumptions`: допущения, принятые при реализации.\n");
    s.push_str("- `open_questions`: вопросы к архитектору.\n");
    s.push_str(
        "- `conflicts_with_prior_decisions`: расхождения с принятыми ранее решениями (ADR, spine).\n\n",
    );
    s.push_str("Архитектурный контекст — `ARCHITECTURE.md`, ограничения — `CONSTRAINTS.yaml`, рубрика приёмки — `RUBRIC.yaml` (при наличии).\n\n");
    s.push_str("## Чеклист перед финальным ответом\n\n");
    s.push_str("- [ ] `SPEC.md` (контракты интерфейсов: входы/выходы, структуры данных, границы ошибок, критерии верификации) заполнен архитектором — сверь реализацию с ним; расхождения фиксируй в `conflicts_with_prior_decisions`, а не молчаливым отступлением.\n");
    s
}

/// Секция «Критерии приёмки» из QAS-сущностей модели репозитория (ADR-007).
///
/// `None` — каталога `<repo>/model/` нет или в нём нет `QAS-*`; сценарии
/// рендерятся в порядке модели (детерминированном), незаполненное поле
/// помечается `—`.
///
/// # Errors
/// Каталог `model/` есть, но модель не разбирается: молчаливый пропуск
/// превратил бы «критерии попадают автоматически» в «иногда попадают».
fn qas_acceptance_section(repo: &Path) -> Result<Option<String>> {
    let model_dir = repo.join("model");
    if !model_dir.is_dir() {
        return Ok(None);
    }
    let model = load_model(&model_dir).map_err(|e| {
        HarnessError::Model(format!(
            "{}: модель для QAS-критериев приёмки не разбирается: {e}",
            model_dir.display()
        ))
    })?;
    let scenarios: Vec<&crate::model::Entity> = model
        .entities
        .iter()
        .filter(|e| e.kind == EntityKind::Qas)
        .collect();
    if scenarios.is_empty() {
        return Ok(None);
    }
    let mut s = String::new();
    s.push_str("## Критерии приёмки (QAS из модели)\n\n");
    s.push_str("Сценарии атрибутов качества из `model/` — обязательная часть приёмки:\n\n");
    for q in scenarios {
        let field = |v: &Option<String>| {
            v.as_deref()
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .unwrap_or("—")
                .to_string()
        };
        let _ = writeln!(
            s,
            "- **{}** ({}): при {} от «{}» к «{}» → {}. Мера: {}.",
            q.id,
            q.title,
            field(&q.stimulus),
            field(&q.source),
            field(&q.artifact),
            field(&q.response),
            field(&q.measure)
        );
    }
    Ok(Some(s))
}

/// План отката по умолчанию (рубрика `handoff_quality::rollback_plan` требует
/// шаги, сигналы-триггеры и владельца решения): точка отката — baseline-коммит,
/// созданный предгейтом [`ensure_git_baseline`].
fn default_rollback(baseline: Option<&str>) -> String {
    let anchor = match baseline {
        Some(h) => format!(
            "Откат: `git reset --hard {h}` (baseline — последний коммит до работы исполнителя; вся его работа приходит одним коммитом поверх).\n"
        ),
        None => "Откат: удалить коммит(ы) исполнителя (`git log` → `git reset --hard <до-исполнителя>`); если репозиторий не под git — удалить созданные за прогон файлы.\n".into(),
    };
    format!(
        "{anchor}\
         Сигналы отката: провал fitness-гейта (`arch-ml control check`), непустой \
         `conflicts_with_prior_decisions`, статус `blocked`.\n\
         Владелец решения об откате — solution-архитектор; исполнитель откат не \
         выполняет и не маскирует проблему обходным редизайном.\n\
         Обратимость: полная — единая точка изменений, коммит исполнителя."
    )
}

/// Рендерит `ROLLBACK.yaml` — машиночитаемый план отката для репетиции на
/// гейте A4 ([`crate::rehearsal`]). Дефолт соответствует [`default_rollback`]:
/// проверка якоря + откат на baseline + verify чистоты дерева. Без git-якоря —
/// пустой план с комментарием (репетиция такого пакета падает с диагностикой).
fn render_rollback_yaml(baseline: Option<&str>) -> String {
    let header = "# План отката handoff-пакета (машиночитаемый) — репетируется на гейте A4:\n\
                  # `arch control gate A4 <repo> --rehearse`. Файл НЕ затирается повторной\n\
                  # генерацией: перед передачей перепишите шаги под фактический план отката\n\
                  # эпика (должен соответствовать разделу «План отката» TASK.md). Шаги с\n\
                  # внешними/деструктивными эффектами репетиция отклоняет — docs/control.md.\n";
    match baseline {
        Some(h) => format!(
            "{header}\
             baseline_commit: \"{h}\"\n\
             steps:\n\
             \x20 - name: якорь-доступен\n\
             \x20   run: git cat-file -t {h}\n\
             \x20 - name: откат-на-baseline\n\
             \x20   run: git reset --hard {h}\n\
             verify: test -z \"$(git status --porcelain --untracked-files=no)\"\n"
        ),
        None => format!(
            "{header}\
             # git недоступен на момент генерации — якоря нет; впишите baseline_commit\n\
             # и шаги вручную, иначе репетиция на A4 упадёт с диагностикой.\n\
             baseline_commit: \"\"\n\
             steps: []\n"
        ),
    }
}

/// Рекомендованный таймаут прогона по маршруту значимости: Critical-эпик
/// (walking skeleton из нескольких модулей) в 30-минутный дефолт адаптера
/// не влезает — прогон обрывался посередине.
///
/// Публичная обёртка — движку планов флота (`fleet_exec`), который берёт
/// таймаут узла из маршрута, когда в плане и MANIFEST его нет.
#[must_use]
pub fn recommended_timeout_for_route(route: Route) -> u64 {
    recommended_timeout(route)
}

fn recommended_timeout(route: Route) -> u64 {
    match route {
        Route::Fast => 1800,
        Route::Standard => 3600,
        Route::Critical => 7200,
    }
}

/// Итог предгейта git: якорь отката и факт инициализации репозитория.
#[derive(Debug, Clone, Default)]
struct GitBaseline {
    /// Короткий хеш baseline-коммита (HEAD на момент генерации пакета).
    hash: Option<String>,
    /// Репозиторий был создан этим вызовом (`git init`).
    initialized: bool,
    /// Есть незакоммиченные изменения ОТСЛЕЖИВАЕМЫХ файлов: откат на
    /// baseline (`reset --hard`) их потеряет (untracked он не трогает).
    dirty_tracked: bool,
}

/// Предгейт handoff: гарантирует git-репозиторий и baseline-коммит-якорь.
///
/// Без git контракт «финальный коммит» невыполним, авто-коммит прогона не
/// работает, а откату не за что зацепиться — поэтому репозиторий
/// инициализируется (`git init`), а при отсутствии коммитов создаётся пустой
/// baseline (`--allow-empty`, идентичность spine-harness). Содержимое каталога
/// в baseline НЕ добавляется осознанно: это дело исполнителя/пользователя.
/// Git недоступен — пакет всё равно собирается, просто без якоря.
fn ensure_git_baseline(repo: &Path) -> GitBaseline {
    let mut initialized = false;
    if git_out(repo, &["rev-parse", "--git-dir"]).is_none() {
        if git_out(repo, &["init", "-q"]).is_none() {
            return GitBaseline::default();
        }
        initialized = true;
    }
    if let Some(head) = git_out(repo, &["rev-parse", "--short", "HEAD"]) {
        let dirty = git_out(repo, &["status", "--porcelain", "--untracked-files=no"])
            .is_some_and(|s| !s.trim().is_empty());
        return GitBaseline {
            hash: Some(head.trim().to_string()),
            initialized,
            dirty_tracked: dirty,
        };
    }
    // Репозиторий без единого коммита — создаём пустой якорь отката.
    let commit = git_out(
        repo,
        &[
            "-c",
            "user.name=spine-harness",
            "-c",
            "user.email=spine-harness@localhost",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "baseline: якорь отката handoff",
        ],
    );
    let hash = commit.and_then(|_| {
        git_out(repo, &["rev-parse", "--short", "HEAD"]).map(|h| h.trim().to_string())
    });
    GitBaseline {
        hash,
        initialized,
        dirty_tracked: false,
    }
}

/// Читает рекомендованный таймаут прогона из MANIFEST.json пакета
/// (None — пакета нет или манифест старый, без поля).
#[must_use]
pub fn recommended_timeout_secs(repo: &Path) -> Option<u64> {
    #[derive(Deserialize)]
    struct ManifestMeta {
        #[serde(default)]
        recommended_timeout_secs: Option<u64>,
    }
    let text = std::fs::read_to_string(repo.join(HANDOFF_DIR).join("MANIFEST.json")).ok()?;
    serde_json::from_str::<ManifestMeta>(&text)
        .ok()?
        .recommended_timeout_secs
}

/// Компилирует epic-context из спецификаций: заголовок с датой и источниками,
/// далее — сжатые рендеры спек; итог усечён до [`EPIC_CONTEXT_MAX_CHARS`].
///
/// Глубина адаптивная: прочие секции рендерятся по [`DEPTH_SHALLOW`] абзацев,
/// но если контекст недобирает до [`EPIC_CONTEXT_MIN_CHARS`] (низ окна рубрики
/// `handoff_quality`, ~800 токенов), спеки перерендериваются глубже
/// ([`DEPTH_DEEP`]) — «реализация без доступа к источникам» требует массы.
///
/// # Errors
/// Спека не читается.
fn compile_epic_context(spec_files: &[PathBuf]) -> Result<String> {
    let mut out = render_epic(spec_files, DEPTH_SHALLOW)?;
    if out.chars().count() < EPIC_CONTEXT_MIN_CHARS {
        out = render_epic(spec_files, DEPTH_DEEP)?;
    }
    if out.chars().count() > EPIC_CONTEXT_MAX_CHARS {
        let notice = "\n\n> **Контекст усечён** до 6000 символов; полные тексты — в файлах-источниках (см. MANIFEST.json).\n";
        let keep = EPIC_CONTEXT_MAX_CHARS.saturating_sub(notice.chars().count());
        let truncated: String = out.chars().take(keep).collect();
        out = truncated;
        out.push_str(notice);
    }
    Ok(out)
}

/// Рендер epic-context на заданной глубине секций (абзацев на прочую секцию;
/// ADR-блоки spine всегда целиком).
fn render_epic(spec_files: &[PathBuf], depth: usize) -> Result<String> {
    let mut out = String::with_capacity(EPIC_CONTEXT_MAX_CHARS);
    out.push_str("# Архитектурный контекст (epic-context)\n\n");
    let _ = write!(out, "Собран: {}\n\n", Utc::now().to_rfc3339());
    out.push_str("Источники:\n");
    for f in spec_files {
        let _ = writeln!(out, "- {}", f.display());
    }
    out.push('\n');
    for f in spec_files {
        let text = std::fs::read_to_string(f).map_err(|e| HarnessError::io(f, e))?;
        let _ = write!(out, "<!-- источник: {} -->\n\n", f.display());
        out.push_str(render_spec(&text, depth).trim_end());
        out.push_str("\n\n");
    }
    Ok(out)
}

/// Рендерит одну спецификацию: секции с полями Binds/Prevents/Rule (ADR-блоки
/// spine) — целиком, прочие секции — заголовок + первые `depth` абзацев.
fn render_spec(text: &str, depth: usize) -> String {
    let mut preamble = String::new();
    let mut sections: Vec<(String, String)> = Vec::new();
    let mut cur: Option<(String, String)> = None;
    let mut in_fence = false;
    for line in text.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with("```") || trimmed.starts_with("~~~") {
            in_fence = !in_fence;
        }
        if !in_fence && line.starts_with('#') {
            if let Some(s) = cur.take() {
                sections.push(s);
            }
            cur = Some((line.trim_end().to_string(), String::new()));
        } else if let Some((_, body)) = cur.as_mut() {
            body.push_str(line);
            body.push('\n');
        } else {
            preamble.push_str(line);
            preamble.push('\n');
        }
    }
    if let Some(s) = cur.take() {
        sections.push(s);
    }

    let mut out = String::new();
    if !preamble.trim().is_empty() {
        out.push_str(&first_paragraphs(&preamble, depth));
        out.push_str("\n\n");
    }
    for (heading, body) in &sections {
        out.push_str(heading);
        out.push_str("\n\n");
        if is_adr_block(body) {
            out.push_str(body.trim());
        } else {
            out.push_str(&first_paragraphs(body, depth));
        }
        out.push_str("\n\n");
    }
    out
}

/// Признак ADR-блока spine: секция содержит поля Binds/Prevents/Rule.
fn is_adr_block(body: &str) -> bool {
    ["Binds:", "Prevents:", "Rule:"]
        .iter()
        .any(|m| body.contains(m))
}

/// Первые `n` абзацев текста (абзацы разделены пустыми строками).
fn first_paragraphs(text: &str, n: usize) -> String {
    let mut paras: Vec<String> = Vec::new();
    let mut cur: Vec<&str> = Vec::new();
    for line in text.lines() {
        if line.trim().is_empty() {
            if !cur.is_empty() {
                paras.push(cur.join("\n"));
                cur.clear();
                if paras.len() >= n {
                    break;
                }
            }
        } else {
            cur.push(line);
        }
    }
    if paras.len() < n && !cur.is_empty() {
        paras.push(cur.join("\n"));
    }
    paras.join("\n\n")
}

/// Признак ADR-файла: md, чьё имя содержит `ADR` или путь содержит `/adr/`.
fn is_adr_file(path: &Path) -> bool {
    if !path
        .extension()
        .is_some_and(|e| e.eq_ignore_ascii_case("md"))
    {
        return false;
    }
    let name_hit = path
        .file_name()
        .and_then(|n| n.to_str())
        .is_some_and(|n| n.contains("ADR"));
    let path_hit = path.to_string_lossy().contains("/adr/");
    name_hit || path_hit
}

/// Итог прогона кодового харнесса.
#[derive(Debug, Clone)]
pub struct HarnessRun {
    /// Имя харнесса.
    pub harness: String,
    /// Код возврата.
    pub exit_code: Option<i32>,
    /// stdout (при прерывании — частичный).
    pub stdout: String,
    /// stderr (при прерывании — частичный).
    pub stderr: String,
    /// Длительность, секунды.
    pub duration_secs: f64,
    /// Как завершился прогон.
    pub termination: Termination,
    /// Авто-коммит незакоммиченных правок исполнителя (None — не потребовался:
    /// дерево чистое, прогон прерван, репозиторий не git или опция выключена).
    pub auto_commit: Option<AutoCommit>,
    /// Механически разобранный JSON-контракт результата из stdout
    /// (валидация схемы — [`parse_result_contract`]).
    pub contract: ContractParse,
}

/// Итог авто-коммита оставшихся после исполнителя правок.
#[derive(Debug, Clone)]
pub struct AutoCommit {
    /// Сколько путей вошло в коммит.
    pub files: usize,
    /// Короткий хеш коммита.
    pub hash: String,
    /// Сообщение коммита.
    pub message: String,
}

/// Способ завершения прогона харнесса.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Termination {
    /// Процесс завершился сам.
    Completed,
    /// Прерван по абсолютному потолку `timeout_secs`.
    AbsoluteTimeout,
    /// Прерван по таймауту тишины: нет вывода и изменений файлов репо
    /// дольше `idle_timeout_secs`.
    IdleTimeout,
    /// Прерван по сигналу отмены (control-канал флота: Kill).
    Canceled,
}

impl std::fmt::Display for Termination {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Completed => write!(f, "завершён"),
            Self::AbsoluteTimeout => write!(f, "абсолютный таймаут"),
            Self::IdleTimeout => write!(f, "idle-таймаут (тишина)"),
            Self::Canceled => write!(f, "отменён (control)"),
        }
    }
}

/// Собирает argv и данные для stdin по режиму [`PromptMode`]:
/// - Positional: `args + [task]`;
/// - Flag: подстановка `{prompt}` в args, иначе `args + [task]`;
/// - Stdin: `args`, задача уходит в stdin.
fn build_argv(cfg: &CodingHarnessConfig, task: &str) -> (Vec<String>, Option<String>) {
    match cfg.prompt_mode {
        PromptMode::Positional => {
            let mut argv = cfg.args.clone();
            argv.push(task.into());
            (argv, None)
        }
        PromptMode::Flag => {
            if cfg.args.iter().any(|a| a.contains("{prompt}")) {
                (
                    cfg.args
                        .iter()
                        .map(|a| a.replace("{prompt}", task))
                        .collect(),
                    None,
                )
            } else {
                let mut argv = cfg.args.clone();
                argv.push(task.into());
                (argv, None)
            }
        }
        PromptMode::Stdin => (cfg.args.clone(), Some(task.into())),
    }
}

/// Максимум удерживаемого вывода каждого потока (stdout/stderr), байт —
/// при превышении хранится хвост (начало важно редко, диагностика в конце).
const OUTPUT_CAP: usize = 256 * 1024;

/// Управление живым прогоном харнесса (control-канал флота).
///
/// `cancel` — Kill: цикл прогона убивает процессную группу и выходит с
/// [`Termination::Canceled`]. `paused` — Pause: цикл отправляет SIGSTOP группе,
/// по снятию — SIGCONT (Resume).
#[derive(Default)]
pub struct HarnessControl {
    /// Отмена прогона.
    pub cancel: std::sync::atomic::AtomicBool,
    /// Пауза прогона.
    pub paused: std::sync::atomic::AtomicBool,
}

/// Запускает кодовый харнесс с задачей в репозитории.
///
/// Бинарь запускается с `cwd = repo` в СОБСТВЕННОЙ процессной группе
/// (`process_group(0)`): харнессы — обёртки вокруг node/python и плодят
/// дочерние процессы; при прерывании убивается ВСЯ группа (TERM → grace →
/// KILL), поэтому сирот (как живой Claude Code после таймаута обёртки)
/// не остаётся.
///
/// Умные таймауты:
/// - абсолютный потолок — `cfg.timeout_secs`;
/// - таймаут тишины — `cfg.idle_timeout_secs` (0 выключает): активность =
///   вывод в stdout/stderr ИЛИ свежие mtime файлов репозитория (молчащий,
///   но работающий харнесс не трогаем).
///
/// При прерывании возвращается Ok с частичным выводом и
/// [`Termination`] ≠ Completed — вызывающий видит, что харнесс успел сделать.
///
/// # Errors
/// Бинарь не найден (с подсказкой по установке/конфигу), сбой запуска/ожидания.
pub async fn run_harness(
    name: &str,
    cfg: &CodingHarnessConfig,
    repo: &Path,
    task: &str,
) -> Result<HarnessRun> {
    run_harness_inner(name, cfg, repo, task, None, None).await
}

/// Streaming-вариант [`run_harness`]: `on_activity` вызывается на каждом
/// heartbeat (вывод stdout/stderr ИЛИ файловая активность в `repo`) — сигнал
/// живости для флота, НЕ сырой вывод (сырой — в `HarnessRun.stdout`/`stderr`
/// и per-agent логе вызывающего). Позволяет оркестратору эмитить
/// `AgentHeartbeat` в стрим, не читая гигабайты stdout большого флота.
pub async fn run_harness_streaming(
    name: &str,
    cfg: &CodingHarnessConfig,
    repo: &Path,
    task: &str,
    on_activity: std::sync::Arc<dyn Fn() + Send + Sync>,
    control: Option<std::sync::Arc<HarnessControl>>,
) -> Result<HarnessRun> {
    run_harness_inner(name, cfg, repo, task, Some(on_activity), control).await
}

#[allow(clippy::too_many_lines)]
async fn run_harness_inner(
    name: &str,
    cfg: &CodingHarnessConfig,
    repo: &Path,
    task: &str,
    on_activity: Option<std::sync::Arc<dyn Fn() + Send + Sync>>,
    control: Option<std::sync::Arc<HarnessControl>>,
) -> Result<HarnessRun> {
    use std::sync::Mutex;
    use tokio::io::AsyncReadExt;

    // Читатели потоков: перекладывают в ограниченные буферы и трогают heartbeat.
    fn spawn_reader<R>(
        mut pipe: R,
        buf: Arc<Mutex<Vec<u8>>>,
        act: Arc<Mutex<Instant>>,
        on_activity: Option<Arc<dyn Fn() + Send + Sync>>,
    ) -> tokio::task::JoinHandle<()>
    where
        R: tokio::io::AsyncRead + Unpin + Send + 'static,
    {
        tokio::spawn(async move {
            let mut chunk = [0u8; 8192];
            loop {
                match pipe.read(&mut chunk).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        let mut b = buf
                            .lock()
                            .unwrap_or_else(std::sync::PoisonError::into_inner);
                        b.extend_from_slice(&chunk[..n]);
                        if b.len() > OUTPUT_CAP {
                            let excess = b.len() - OUTPUT_CAP;
                            b.drain(..excess);
                        }
                        drop(b);
                        *act.lock()
                            .unwrap_or_else(std::sync::PoisonError::into_inner) = Instant::now();
                        if let Some(cb) = &on_activity {
                            cb();
                        }
                    }
                }
            }
        })
    }

    let (argv, stdin_data) = build_argv(cfg, task);
    let mut cmd = Command::new(&cfg.binary);
    cmd.args(&argv).current_dir(repo);
    // Whitelist окружения: чужие переменные хоста (модели, прокси, ключи)
    // не протекают в дочерний процесс; `env` адаптера — поверх всегда.
    if !cfg.env_allow.is_empty() {
        cmd.env_clear();
        for name in &cfg.env_allow {
            if let Ok(v) = std::env::var(name) {
                cmd.env(name, v);
            }
        }
    }
    cmd.envs(&cfg.env)
        // Своя процессная группа: убивать будем группу целиком.
        .process_group(0)
        .kill_on_drop(true)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if stdin_data.is_some() {
        cmd.stdin(Stdio::piped());
    } else {
        cmd.stdin(Stdio::null());
    }

    let started = Instant::now();
    let mut child = cmd.spawn().map_err(|e| {
        if e.kind() == ErrorKind::NotFound {
            HarnessError::Harness(format!(
                "бинарь '{}' не найден: установите {} или поправьте config.toml [harnesses.{name}]",
                cfg.binary, cfg.binary
            ))
        } else {
            HarnessError::Harness(format!("не удалось запустить '{}': {e}", cfg.binary))
        }
    })?;
    let pid = child.id().unwrap_or(0);

    // Активность: последний вывод ИЛИ свежая файловая активность в репо.
    let activity = Arc::new(Mutex::new(Instant::now()));
    let stdout_buf = Arc::new(Mutex::new(Vec::<u8>::new()));
    let stderr_buf = Arc::new(Mutex::new(Vec::<u8>::new()));

    let mut readers = Vec::new();
    if let Some(out) = child.stdout.take() {
        readers.push(spawn_reader(
            out,
            stdout_buf.clone(),
            activity.clone(),
            on_activity.clone(),
        ));
    }
    if let Some(err) = child.stderr.take() {
        readers.push(spawn_reader(
            err,
            stderr_buf.clone(),
            activity.clone(),
            on_activity.clone(),
        ));
    }

    // Пишем задачу в stdin отдельной задачей, чтобы не было дедлока на
    // заполненном pipe-буфере, пока читаются stdout/stderr.
    let writer = match (child.stdin.take(), stdin_data) {
        (Some(mut pipe), Some(data)) => Some(tokio::spawn(async move {
            // Ошибка записи осознанно игнорируется: процесс вправе закрыть stdin раньше.
            let _ = pipe.write_all(data.as_bytes()).await;
            // drop(pipe) закрывает stdin — EOF для процесса.
        })),
        _ => None,
    };

    let abs_limit = Duration::from_secs(cfg.timeout_secs.max(1));
    let idle_limit =
        (cfg.idle_timeout_secs > 0).then(|| Duration::from_secs(cfg.idle_timeout_secs));
    // Файловый heartbeat: скан не чаще раза в 15 с (и не реже четверти
    // idle-окна, чтобы мелкие окна тоже ловили активность); базовая отсечка —
    // старт прогона (старые файлы репо активностью не считаются).
    let scan_interval = idle_limit.map_or(Duration::from_secs(15), |i| {
        (i / 4).clamp(Duration::from_secs(1), Duration::from_secs(15))
    });
    let mut last_scan = std::time::SystemTime::now();
    let mut scan_due = Instant::now();
    // Состояние паузы прогона (SIGSTOP/SIGCONT при переходах Pause/Resume).
    let mut stopped = false;

    let termination = loop {
        match child.try_wait() {
            Ok(Some(_)) => break Termination::Completed,
            Ok(None) => {}
            Err(e) => {
                return Err(HarnessError::Harness(format!(
                    "сбой ожидания '{}': {e}",
                    cfg.binary
                )));
            }
        }
        // Сигнал отмены (control-канал флота): Kill → убиваем группу и выходим.
        if control
            .as_ref()
            .is_some_and(|c| c.cancel.load(std::sync::atomic::Ordering::Relaxed))
        {
            kill_process_group(pid, &mut child).await;
            break Termination::Canceled;
        }
        // Пауза (Pause/Resume): SIGSTOP при включении, SIGCONT при снятии.
        let want_pause = control
            .as_ref()
            .is_some_and(|c| c.paused.load(std::sync::atomic::Ordering::Relaxed));
        if want_pause != stopped {
            signal_process_group(pid, if want_pause { "-STOP" } else { "-CONT" });
            stopped = want_pause;
        }
        let elapsed = started.elapsed();
        if elapsed >= abs_limit {
            kill_process_group(pid, &mut child).await;
            break Termination::AbsoluteTimeout;
        }
        if let Some(idle) = idle_limit {
            if Instant::now() >= scan_due {
                scan_due = Instant::now() + scan_interval;
                let scan_start = std::time::SystemTime::now();
                if repo_changed_since(repo, last_scan) {
                    *activity
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner) = Instant::now();
                    if let Some(cb) = &on_activity {
                        cb();
                    }
                }
                last_scan = scan_start;
            }
            let silent_for = activity
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .elapsed();
            if silent_for >= idle {
                kill_process_group(pid, &mut child).await;
                break Termination::IdleTimeout;
            }
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    };

    if let Some(w) = &writer {
        w.abort();
    }
    // Читатели завершаются по EOF на закрытых пайпах; страховочный лимит.
    for r in readers {
        let _ = tokio::time::timeout(Duration::from_secs(2), r).await;
    }

    let take = |b: &Arc<Mutex<Vec<u8>>>| {
        String::from_utf8_lossy(&b.lock().unwrap_or_else(std::sync::PoisonError::into_inner))
            .into_owned()
    };
    // Страховка финализации: контракт TASK.md требует от исполнителя
    // финальный git-коммит; не сделал — фиксируем сами, иначе работа
    // теряется для оркестратора (результат забирается из git).
    let auto_commit = if termination == Termination::Completed && cfg.auto_commit {
        auto_commit_leftovers(repo, name, task)
    } else {
        None
    };
    let stdout = take(&stdout_buf);
    let stderr = take(&stderr_buf);
    // Контракт разбирается один раз на стороне запуска — механически,
    // а не эвристикой у потребителей.
    let contract = parse_result_contract(&stdout);
    Ok(HarnessRun {
        harness: name.into(),
        exit_code: child.try_wait().ok().flatten().and_then(|s| s.code()),
        stdout,
        stderr,
        duration_secs: started.elapsed().as_secs_f64(),
        termination,
        auto_commit,
        contract,
    })
}

/// Выполняет git-команду в репозитории; None — команда упала или stderr.
fn git_out(repo: &Path, args: &[&str]) -> Option<String> {
    let out = std::process::Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// Коммитит незакоммиченные правки исполнителя (кроме `.arch-handoff/` и
/// мусора `__pycache__/`/`*.pyc`/`.pytest_cache/`). None — не git-репозиторий
/// или дерево чистое. Сбой коммита не роняет прогон: исполнитель мог
/// закоммитить частично, диагностику видно по `git status`.
fn auto_commit_leftovers(repo: &Path, harness: &str, task: &str) -> Option<AutoCommit> {
    // Не git-репозиторий — нечего фиксировать.
    git_out(repo, &["rev-parse", "--git-dir"])?;
    // Добавляем всё, кроме служебного пакета и типичного мусора интерпретеров.
    git_out(
        repo,
        &[
            "add",
            "-A",
            "--",
            ".",
            ":!.arch-handoff",
            ":(exclude,glob)**/__pycache__/**",
            ":(exclude,glob)**/*.pyc",
            ":(exclude,glob)**/.pytest_cache/**",
        ],
    )?;
    let staged = git_out(repo, &["diff", "--cached", "--name-only"])?;
    let files = staged.lines().filter(|l| !l.trim().is_empty()).count();
    if files == 0 {
        return None;
    }
    let first_line = task.lines().next().unwrap_or("задача").trim();
    let mut title: String = first_line.chars().take(60).collect();
    if first_line.chars().count() > 60 {
        title.push('…');
    }
    let message = format!("harness({harness}): {title}");
    // Явная идентичность: в свежих worktree/контейнерах user.name/user.email
    // часто не настроены, и без этого коммит падает.
    git_out(
        repo,
        &[
            "-c",
            "user.name=spine-harness",
            "-c",
            "user.email=spine-harness@localhost",
            "commit",
            "-q",
            "-m",
            &message,
        ],
    )?;
    let hash = git_out(repo, &["rev-parse", "--short", "HEAD"])?
        .trim()
        .to_string();
    Some(AutoCommit {
        files,
        hash,
        message,
    })
}

/// Мягко, затем жёстко завершает процессную группу `pid` (TERM → 3 с → KILL).
/// Убивает и дочерние процессы харнесса — сирот после таймаута не остаётся.
async fn kill_process_group(pid: u32, child: &mut tokio::process::Child) {
    if pid > 0 {
        // kill из coreutils есть всегда; unsafe/libc запрещены линтом проекта.
        // ВАЖНО: разделитель `--` обязателен — procps `/bin/kill -TERM -PGID`
        // без него молча (rc=0!) трактует отрицательное число как опцию и
        // никого не сигналит (проверено опытом; bash-builtin kill работал и так).
        let _ = std::process::Command::new("kill")
            .args(["-TERM", "--", &format!("-{pid}")])
            .status();
        for _ in 0..10 {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return;
            }
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
        let _ = std::process::Command::new("kill")
            .args(["-KILL", "--", &format!("-{pid}")])
            .status();
    } else {
        // pgid неизвестен (теоретический случай) — хотя бы самого ребёнка.
        let _ = child.kill().await;
        return;
    }
    // Забираем zombie, чтобы try_wait наверняка отдал статус.
    let _ = tokio::time::timeout(Duration::from_secs(3), child.wait()).await;
}

/// Сигналит процессной группе (SIGSTOP/SIGCONT) — без unsafe, через coreutils
/// `kill` с обязательным `--` перед отрицательным PGID.
fn signal_process_group(pid: u32, signal: &str) {
    if pid > 0 {
        let _ = std::process::Command::new("kill")
            .args([signal, "--", &format!("-{pid}")])
            .status();
    }
}

/// Есть ли в репозитории файлы, изменённые после `since` (heartbeat активности
/// молчащего харнесса). Служебные/тяжёлые каталоги пропускаются; лимит —
/// 8000 записей, глубина 8 (дорогое сканирование не нужно: свежие файлы
/// почти всегда наверху).
fn repo_changed_since(repo: &Path, since: std::time::SystemTime) -> bool {
    const SKIP: [&str; 6] = [
        ".git",
        "target",
        "node_modules",
        "dist",
        "__pycache__",
        ".next",
    ];
    let mut seen = 0usize;
    for entry in walkdir::WalkDir::new(repo)
        .max_depth(8)
        .into_iter()
        .filter_entry(|e| {
            e.depth() == 0 || !SKIP.contains(&e.file_name().to_string_lossy().as_ref())
        })
        .filter_map(std::result::Result::ok)
    {
        seen += 1;
        if seen > 8000 {
            break;
        }
        if !entry.file_type().is_file() {
            continue;
        }
        let fresh = entry
            .metadata()
            .ok()
            .and_then(|m| m.modified().ok())
            .is_some_and(|mt| mt > since);
        if fresh {
            return true;
        }
    }
    false
}

/// Инструмент `handoff_create`: генерация handoff-пакета из агентного цикла.
struct HandoffCreateTool {
    /// Конфигурация (пути к рубрикам, модель по умолчанию).
    cfg: Config,
}

#[async_trait]
impl Tool for HandoffCreateTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "handoff_create".into(),
            description: "Сгенерировать handoff-пакет (.arch-handoff/: TASK.md, ARCHITECTURE.md, CONSTRAINTS.yaml, SPEC.md — шаблон верифицируемых контрактов интерфейсов, MANIFEST.json, adr/) для передачи задачи кодовому харнессу. Предгейт: гарантирует git-репозиторий и baseline-коммит (якорь отката); TASK.md включает план отката и требование финального git-коммита; MANIFEST несёт рекомендованный таймаут прогона по маршруту значимости (подхватывает harness_run)".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Корень репозитория (относительно cwd или абсолютный)"},
                    "task": {"type": "string", "description": "Формулировка задачи для кодового харнесса"},
                    "spec": {"type": "array", "items": {"type": "string"}, "description": "Пути к спецификациям/ADR (md), опционально"},
                    "rollback": {"type": "string", "description": "Явный план отката (шаги, сигналы, владелец решения); по умолчанию — откат на baseline-коммит"},
                    "route": {"type": "string", "enum": ["fast", "standard", "critical"], "description": "Маршрут значимости из significance_score: задаёт рекомендованный таймаут прогона (fast=1800с, standard=3600с, critical=7200с); по умолчанию standard"}
                },
                "required": ["repo", "task"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let Some(repo) = args.get("repo").and_then(Value::as_str) else {
            return Ok(ToolOutput::err(
                "handoff_create: обязательный аргумент 'repo' (string) отсутствует",
            ));
        };
        let Some(task) = args.get("task").and_then(Value::as_str) else {
            return Ok(ToolOutput::err(
                "handoff_create: обязательный аргумент 'task' (string) отсутствует",
            ));
        };
        let spec: Vec<PathBuf> = args
            .get("spec")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(|s| ctx.resolve(s))
                    .collect()
            })
            .unwrap_or_default();
        let repo = ctx.resolve(repo);
        let rollback = args.get("rollback").and_then(Value::as_str);
        let route = match args.get("route").and_then(Value::as_str) {
            Some(r) => match r.parse::<Route>() {
                Ok(route) => route,
                Err(e) => return Ok(ToolOutput::err(format!("handoff_create: {e}"))),
            },
            None => Route::Standard,
        };
        match generate_handoff(&repo, task, &spec, &self.cfg, rollback, route) {
            Ok(packet) => {
                let files = packet
                    .files
                    .iter()
                    .map(|f| format!("- {}", f.display()))
                    .collect::<Vec<_>>()
                    .join("\n");
                let mut out = format!(
                    "Handoff-пакет создан: {}\nEpic-context: ~{} токенов.\nФайлы:\n{files}",
                    packet.dir.display(),
                    packet.epic_context_tokens
                );
                // Предгейт git: якорь отката и факт инициализации.
                match &packet.baseline {
                    Some(h) if packet.git_initialized => {
                        let _ = write!(
                            out,
                            "\nGit: репозиторий инициализирован, baseline-коммит {h} (якорь отката в TASK.md)."
                        );
                    }
                    Some(h) => {
                        let _ = write!(out, "\nGit: baseline-коммит {h} (якорь отката в TASK.md).");
                    }
                    None => {
                        out.push_str(
                            "\nВНИМАНИЕ: git недоступен — якоря отката нет; контракт \
                             финального коммита и авто-коммит прогона работать не будут.",
                        );
                    }
                }
                let _ = write!(
                    out,
                    "\nМаршрут: {route} → рекомендованный timeout_secs={} \
                     (harness_run подхватит из MANIFEST.json, если не задан явно).",
                    packet.recommended_timeout_secs
                );
                if packet.git_dirty_tracked {
                    out.push_str(
                        "\nВНИМАНИЕ: есть незакоммиченные изменения отслеживаемых файлов — \
                         откат на baseline (`git reset --hard`) их потеряет: закоммитьте \
                         заранее или осознанно включите в задачу.",
                    );
                }
                // Окно рубрики handoff_quality — 800–1500 токенов.
                if packet.epic_context_tokens < EPIC_CONTEXT_MIN_CHARS / 4 {
                    let _ = write!(
                        out,
                        "\nВНИМАНИЕ: epic-context ~{} токенов — ниже окна рубрики (800–1500). \
                         Сценарий «реализация без доступа к источникам» не выполняется: \
                         добавьте спеки через 'spec' или расширьте источники.",
                        packet.epic_context_tokens
                    );
                }
                out.push_str(
                    "\nНапоминание: CONSTRAINTS.yaml — стековая заготовка; перед передачей \
                     перепишите правила под spine-инварианты (AD-n) эпика.",
                );
                Ok(ToolOutput::ok(out))
            }
            Err(e) => Ok(ToolOutput::err(format!("handoff_create: {e}"))),
        }
    }
}

/// Лимит вывода `harness_run` (stdout + stderr), символов.
const HARNESS_RUN_MAX_CHARS: usize = 24 * 1024;

/// Статус из контракта результата (схема TASK.md).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContractStatus {
    /// Выполнено полностью.
    Complete,
    /// Частично.
    Partial,
    /// Заблокировано (интеграция невозможна до разбора).
    Blocked,
}

impl ContractStatus {
    fn parse(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "complete" => Some(Self::Complete),
            "partial" => Some(Self::Partial),
            "blocked" => Some(Self::Blocked),
            _ => None,
        }
    }

    /// Строковое представление как в контракте.
    #[must_use]
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Complete => "complete",
            Self::Partial => "partial",
            Self::Blocked => "blocked",
        }
    }
}

/// Механически разобранный и проверенный по схеме контракт результата.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResultContract {
    /// Статус прогона.
    pub status: ContractStatus,
    /// Допущения исполнителя.
    pub assumptions: Vec<String>,
    /// Открытые вопросы к архитектору.
    pub open_questions: Vec<String>,
    /// Расхождения с принятыми решениями (ADR/spine) — останавливают интеграцию.
    pub conflicts: Vec<String>,
}

/// Исход механического разбора контракта результата.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContractParse {
    /// Контракт найден и валиден по схеме.
    Valid(ResultContract),
    /// Блок со `status` найден, но схема нарушена (причина).
    Invalid(String),
    /// Контракта в выводе нет.
    Missing,
}

/// Проверяет кандидата по схеме контракта: `status` строго из
/// complete|partial|blocked; списки опциональны (дефолт — пустые), но если
/// присутствуют — обязаны быть массивами (элементы приводятся к строкам).
fn validate_contract(v: &Value) -> std::result::Result<ResultContract, String> {
    let status_raw = v
        .get("status")
        .and_then(Value::as_str)
        .ok_or("поле `status` отсутствует или не строка")?;
    let status = ContractStatus::parse(status_raw)
        .ok_or_else(|| format!("status='{status_raw}' вне complete|partial|blocked"))?;
    let list = |key: &str| -> std::result::Result<Vec<String>, String> {
        match v.get(key) {
            None => Ok(Vec::new()),
            Some(Value::Array(items)) => Ok(items
                .iter()
                .map(|i| match i {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .collect()),
            Some(_) => Err(format!("поле `{key}` не массив")),
        }
    };
    Ok(ResultContract {
        status,
        assumptions: list("assumptions")?,
        open_questions: list("open_questions")?,
        conflicts: list("conflicts_with_prior_decisions")?,
    })
}

/// Механический разбор контракта результата из stdout харнесса
/// (замена текстовой эвристике «последний `` ```json `` со `status`»):
///
/// 1. fenced `` ```json ``-блоки с конца вывода (контракт обязан идти последним);
///    блок со `status`, не парсящийся как JSON, — это Invalid, а не промах;
/// 2. запасной путь: голый JSON-объект в хвосте вывода (модели иногда роняют
///    fence) — перебор `{`-позиций последних 4 КБ с конца.
///
/// Найденный кандидат валидируется по схеме [`validate_contract`].
#[must_use]
pub fn parse_result_contract(stdout: &str) -> ContractParse {
    let mut invalid: Option<String> = None;
    let mut blocks = Vec::new();
    let mut rest = stdout;
    while let Some(start) = rest.find("```json") {
        let after = &rest[start + "```json".len()..];
        match after.find("```") {
            Some(end) => {
                blocks.push(after[..end].trim());
                rest = &after[end + 3..];
            }
            None => break,
        }
    }
    for block in blocks.into_iter().rev() {
        match serde_json::from_str::<Value>(block) {
            Ok(v) if v.get("status").is_some() => {
                return match validate_contract(&v) {
                    Ok(c) => ContractParse::Valid(c),
                    Err(e) => ContractParse::Invalid(e),
                };
            }
            Err(e) if block.contains("\"status\"") => {
                invalid = Some(format!("невалидный JSON в ```json-блоке со status: {e}"));
            }
            Ok(_) | Err(_) => {}
        }
    }
    // Голый JSON в хвосте (fence уронен): перебираем `{` с конца хвоста.
    // `floor_char_boundary` стабилизирован в 1.91 — выше MSRV 1.85: идём к
    // ближайшей границе символа вручную (эквивалент по семантике).
    let mut tail_at = stdout.len().saturating_sub(4096);
    while !stdout.is_char_boundary(tail_at) {
        tail_at -= 1;
    }
    let tail = &stdout[tail_at..];
    let braces: Vec<usize> = tail.match_indices('{').map(|(i, _)| i).collect();
    for i in braces.into_iter().rev().take(8) {
        let cand = tail[i..].trim();
        if !cand.contains("\"status\"") {
            continue;
        }
        if let Ok(v) = serde_json::from_str::<Value>(cand) {
            if v.get("status").is_some() {
                return match validate_contract(&v) {
                    Ok(c) => ContractParse::Valid(c),
                    Err(e) => ContractParse::Invalid(e),
                };
            }
        }
    }
    match invalid {
        Some(e) => ContractParse::Invalid(e),
        None => ContractParse::Missing,
    }
}

/// Инструмент `harness_run`: прогон handoff-пакета (или явной задачи)
/// кодовым харнессом — без импровизации через bash (квотинг, permission-
/// промпты, короткие таймауты bash — частые точки отказа такой импровизации).
struct HarnessRunTool {
    /// Конфигурация (адаптеры харнессов).
    cfg: Config,
}

impl HarnessRunTool {
    /// Живой конфиг: файл перечитывается при каждом вызове — правки
    /// `[harnesses.*]` в config.toml подхватываются без перезапуска сессии
    /// (инцидент: агент исправил адаптеры в файле, а прогон шёл со снапшота
    /// конфига, загруженного при старте процесса). Перечитывается только
    /// файл, из которого конфиг был загружен (`loaded_from`): снапшот без
    /// файла (тесты, чистые дефолты) окружение не подхватывает. При сбое
    /// чтения — снапшот процесса.
    fn live_config(&self) -> Config {
        match self.cfg.loaded_from.as_deref() {
            Some(path) => Config::load(Some(path)).unwrap_or_else(|_| self.cfg.clone()),
            None => self.cfg.clone(),
        }
    }

    /// Фоновый прогон: задача регистрируется в общем реестре фоновых задач
    /// (префикс `hr-`, видна в `subagent_list`), исполнение — в отдельной
    /// tokio-задаче; инструмент возвращается немедленно. Полный лог пишется
    /// файлом (`reports_dir/harness/<id>.log`), в реестр — усечённый отчёт
    /// (лимит [`crate::subagent::REPORT_MAX_CHARS`], как у субагентов).
    /// Прерывание хода (Esc/Alt+Enter) фоновый прогон НЕ затрагивает —
    /// задача не привязана к токену отмены сессии.
    fn launch_background(
        &self,
        name: &str,
        hcfg: CodingHarnessConfig,
        repo: PathBuf,
        task: String,
        note: &str,
        ctx: &ToolContext,
    ) -> ToolOutput {
        let Some(registry) = &ctx.subagents else {
            return ToolOutput::err(
                "harness_run background: реестр фоновых задач не подключён \
                 (headless-режим без TUI/раннера) — запустите без background",
            );
        };
        if registry.running() >= registry.capacity() {
            return ToolOutput::err(format!(
                "все слоты фоновых задач заняты ({}); дождитесь завершения — subagent_list",
                registry.capacity()
            ));
        }
        let id = registry.next_id("hr");
        registry.insert(crate::subagent::SubagentTask {
            id: id.clone(),
            agent: format!("harness:{name}"),
            task: task.chars().take(2000).collect(),
            status: crate::subagent::TaskStatus::Running,
            report: String::new(),
            started_at: crate::subagent::now_iso(),
            finished_at: None,
        });
        let report_path = ctx
            .config
            .paths
            .reports_dir
            .join("harness")
            .join(format!("{id}.log"));
        let registry = registry.clone();
        let run_id = id.clone();
        let run_name = name.to_string();
        let note_run = note.to_string();
        let report_path_run = report_path.clone();
        tokio::spawn(async move {
            let out = execute_run(&run_name, &hcfg, &repo, &task, note_run).await;
            let status = if out.is_error {
                crate::subagent::TaskStatus::Failed
            } else {
                crate::subagent::TaskStatus::Done
            };
            // Полный лог — файлом (best effort: отчёт живёт и в реестре).
            if let Some(dir) = report_path_run.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            if let Err(e) = std::fs::write(&report_path_run, &out.content) {
                tracing::warn!(
                    "лог фонового прогона не записан {}: {e}",
                    report_path_run.display()
                );
            }
            let report: String = out
                .content
                .chars()
                .take(crate::subagent::REPORT_MAX_CHARS)
                .collect();
            registry.finish(&run_id, status, report);
        });
        ToolOutput::ok(format!(
            "{note}Прогон харнесса '{name}' запущен в ФОНЕ: {id}. Этот ход агента НЕ ждёт \
             завершения — агент остаётся доступен пользователю. Статус — subagent_list, \
             результат — subagent_result(id=\"{id}\"), полный лог — {}. \
             Сообщи пользователю, что прогон идёт в фоне, и продолжай диалог.",
            report_path.display()
        ))
    }
}

/// Enforcement `[fleet] require_worktree`: прогон кодового харнесса не имеет
/// прямого пути в основное дерево репозитория — рабочим каталогом прогона
/// становится изолированный git worktree (ветка `arch/<slug>-<timestamp>`,
/// фабрика [`crate::worktree`]). Handoff-пакет `.arch-handoff/` по контракту
/// в git не коммитится, поэтому копируется в worktree как есть. Интеграция
/// результата — только через гейт владельца (`arch fleet merge <run-id>`).
///
/// Возвращает `None`, если enforcement выключен (`require_worktree = false`,
/// дефолт — поведение прежних версий), иначе `Some((каталог прогона, run-id))`.
///
/// # Errors
/// `require_worktree = true`, а каталог — не git-репозиторий (понятная ошибка
/// ДО запуска харнесса); сбой создания worktree.
pub async fn enforce_run_worktree(
    cfg: &Config,
    repo: &Path,
    slug: &str,
) -> Result<Option<(PathBuf, String)>> {
    if !cfg.fleet.require_worktree {
        return Ok(None);
    }
    // Kebab-слаг из имени харнесса (валидатор worktree: [a-z0-9-], ≤ 48
    // символов); timestamp «-yyyymmddhhmmss» занимает 15 — слаг до 32.
    let slug: String = slug
        .to_ascii_lowercase()
        .chars()
        .map(|c| {
            if c.is_ascii_lowercase() || c.is_ascii_digit() {
                c
            } else {
                '-'
            }
        })
        .take(32)
        .collect();
    let run_id = format!(
        "{}-{}",
        slug.trim_matches('-'),
        Utc::now().format("%Y%m%d%H%M%S")
    );
    let dir = crate::worktree::create(cfg, repo, &run_id, None).await?;
    // `.arch-handoff/` не входит в коммиты (контракт TASK.md) — в свежем
    // worktree его нет; копируем, иначе исполнитель потеряет пакет задачи.
    let handoff = repo.join(HANDOFF_DIR);
    if handoff.is_dir() && !dir.join(HANDOFF_DIR).exists() {
        copy_dir(&handoff, &dir.join(HANDOFF_DIR))?;
    }
    Ok(Some((dir, run_id)))
}

/// Worktree узла плана флота с ДЕТЕРМИНИРОВАННЫМ именем `<run-id>-<node-id>`.
///
/// Отличие от [`enforce_run_worktree`] — имя не содержит timestamp: повторный
/// прогон узла (`fleet resume`) переиспользует то же дерево и ветку
/// (`worktree::get_or_create`), поэтому возобновление идемпотентно (ADR-042).
/// Handoff-пакет копируется так же, как при обычном прогоне.
///
/// # Errors
/// Не git-репозиторий; сбой git при создании worktree.
pub async fn enforce_node_worktree(
    cfg: &Config,
    repo: &Path,
    run_id: &str,
    node_id: &str,
) -> Result<Option<(PathBuf, String)>> {
    if !cfg.fleet.require_worktree {
        return Ok(None);
    }
    let slug = |raw: &str, keep: usize| -> String {
        raw.to_ascii_lowercase()
            .chars()
            .map(|c| {
                if c.is_ascii_lowercase() || c.is_ascii_digit() {
                    c
                } else {
                    '-'
                }
            })
            .take(keep)
            .collect::<String>()
            .trim_matches('-')
            .to_string()
    };
    let run_slug = slug(run_id, 20);
    let node_slug = slug(node_id, 24);
    let name = format!("{run_slug}-{node_slug}");
    let dir = crate::worktree::get_or_create(cfg, repo, &name, None).await?;
    let handoff = repo.join(HANDOFF_DIR);
    if handoff.is_dir() && !dir.join(HANDOFF_DIR).exists() {
        copy_dir(&handoff, &dir.join(HANDOFF_DIR))?;
    }
    Ok(Some((dir, name)))
}

/// Рекурсивное копирование каталога (handoff-пакет в worktree прогона).
fn copy_dir(src: &Path, dst: &Path) -> Result<()> {
    std::fs::create_dir_all(dst).map_err(|e| HarnessError::io(dst, e))?;
    for entry in std::fs::read_dir(src).map_err(|e| HarnessError::io(src, e))? {
        let entry = entry.map_err(|e| HarnessError::io(src, e))?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if from.is_dir() {
            copy_dir(&from, &to)?;
        } else {
            std::fs::copy(&from, &to).map_err(|e| HarnessError::io(&to, e))?;
        }
    }
    Ok(())
}

#[async_trait]
impl Tool for HarnessRunTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "harness_run".into(),
            description: "Запустить кодовый харнесс на репозитории и вернуть его вывод. \
                Обычно следует за handoff_create: задача читается из \
                <repo>/.arch-handoff/TASK.md (или передаётся явно). Запуск идёт через \
                настроенный адаптер [harnesses.<имя>] (режим prompt, env); config.toml \
                перечитывается на каждый вызов — правки адаптеров применяются без \
                перезапуска сессии. Умные таймауты: \
                абсолютный потолок 30 мин (по умолчанию) + таймаут тишины 10 мин — прогон \
                прерывается, только если харнесс не выводит и не меняет файлы репозитория; \
                при прерывании убивается вся процессная группа (сирот не остаётся) и \
                возвращается частичный вывод. НЕ занижайте timeout_secs: значения ниже \
                600 поднимаются до 600 — кодовый харнесс за меньшее время почти никогда \
                не успевает. stdout/stderr и код возврата захватываются, JSON-контракт \
                результата (status/assumptions/open_questions) разбирается механически \
                (валидация схемы, эскалация blocked/conflicts). \
                НЕ запускать харнесс через bash — там промпт ломается о квотинг, таймаут \
                слишком короткий, а env-scrub прячет от команды переменные *_KEY/*_TOKEN, \
                через которые харнесс может авторизовываться. \
                background=true — прогон в фоне: инструмент возвращается сразу (id задачи \
                hr-*), агент остаётся доступным пользователю; статус — subagent_list, \
                результат — subagent_result(id). Используй background для длинных прогонов \
                и когда пользователь продолжает диалог во время работы харнесса."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "harness": {
                        "type": "string",
                        "description": "Имя харнесса: claude-code, qwen-code, openclaw, hermes, theseus, codewhale, kimi-code"
                    },
                    "repo": {
                        "type": "string",
                        "description": "Корень репозитория (относительно cwd или абсолютный)"
                    },
                    "task": {
                        "type": "string",
                        "description": "Явная задача; если не задана — читается <repo>/.arch-handoff/TASK.md"
                    },
                    "timeout_secs": {
                        "type": "integer",
                        "description": "Переопределить АБСОЛЮТНЫЙ таймаут адаптера, секунды (минимум 600 — меньшие значения поднимаются; максимум 7200). Тишина контролируется отдельно (idle_timeout_secs адаптера, по умолчанию 600)",
                        "minimum": 600,
                        "maximum": 7200
                    },
                    "background": {
                        "type": "boolean",
                        "description": "true — прогон в фоне (немедленный возврат, задача hr-* в реестре фоновых задач; результат забирается через subagent_result). false/отсутствует — синхронно: ход агента ждёт завершения прогона"
                    }
                },
                "required": ["harness", "repo"]
            }),
        }
    }

    /// Прогон кодового харнесса может идти до 7200 с (потолок аргумента
    /// `timeout_secs`) плюс запас на групповое завершение и сбор вывода;
    /// берём максимум из адаптеров конфига — иначе агентный цикл обрывает
    /// длинный прогон раньше собственного таймаута адаптера (инцидент 11-24).
    fn timeout_secs(&self) -> u64 {
        let live = self.live_config();
        let adapter_max = live
            .harnesses
            .values()
            .map(|h| h.timeout_secs)
            .max()
            .unwrap_or(0);
        adapter_max.max(7200) + 120
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let Some(name) = args.get("harness").and_then(Value::as_str) else {
            return Ok(ToolOutput::err(
                "harness_run: обязательный аргумент 'harness' (string) отсутствует",
            ));
        };
        let Some(repo) = args.get("repo").and_then(Value::as_str) else {
            return Ok(ToolOutput::err(
                "harness_run: обязательный аргумент 'repo' (string) отсутствует",
            ));
        };
        // Адаптеры читаем из ЖИВОГО конфига: правки config.toml в ходе
        // сессии применяются немедленно, без перезапуска агента.
        let live = self.live_config();
        let Some(hcfg) = live.harnesses.get(name) else {
            return Ok(ToolOutput::err(format!(
                "harness_run: харнесс '{name}' не настроен. Известные: {}; \
                 адаптеры — в config.toml [harnesses.<имя>]",
                known().join(", ")
            )));
        };
        let repo = ctx.resolve(repo);
        // [fleet] require_worktree: путь в main для прогона закрыт платформенно —
        // работа идёт в изолированном git worktree, интеграция результата —
        // только гейтом владельца (`arch fleet merge <run-id>`).
        let (repo, gate_note) = match enforce_run_worktree(&live, &repo, name).await {
            Ok(Some((dir, run_id))) => (
                dir,
                format!(
                    "Изоляция прогона ([fleet] require_worktree): run-id '{run_id}', рабочий \
                     каталог — git worktree ветки arch/{run_id}; основное дерево НЕ изменяется. \
                     Интеграция — только владельцем: `arch fleet merge {run_id} --owner-approve` \
                     (влить) или `arch worktree drop {run_id}` (отклонить). Сообщи это \
                     пользователю в финальном ответе.\n"
                ),
            ),
            Ok(None) => (repo, String::new()),
            Err(e) => return Ok(ToolOutput::err(format!("harness_run: {e}"))),
        };
        let task = if let Some(t) = args.get("task").and_then(Value::as_str) {
            t.to_string()
        } else {
            let path = repo.join(HANDOFF_DIR).join("TASK.md");
            match std::fs::read_to_string(&path) {
                Ok(t) => t,
                Err(e) => {
                    return Ok(ToolOutput::err(format!(
                        "harness_run: нет аргумента 'task' и не читается {}: {e}. \
                         Сначала handoff_create или передайте task явно",
                        path.display()
                    )));
                }
            }
        };
        // Переопределение таймаута — копией конфига адаптера. Минимум 600 с:
        // модели склонны занижать таймаут («успеет за 5 минут»), а кодовый
        // харнесс на реальной задаче работает дольше — ранний таймаут
        // обрывал прогон и оставлял репозиторий в полусобранном состоянии.
        let mut hcfg = hcfg.clone();
        let mut note = gate_note;
        if let Some(t) = args.get("timeout_secs").and_then(Value::as_u64) {
            if t < MIN_HARNESS_TIMEOUT_SECS {
                let _ = writeln!(
                    note,
                    "timeout_secs={t} поднят до {MIN_HARNESS_TIMEOUT_SECS} (минимум для кодового харнесса)."
                );
            }
            hcfg.timeout_secs = t.clamp(MIN_HARNESS_TIMEOUT_SECS, 7200);
        } else if let Some(t) = recommended_timeout_secs(&repo) {
            // Таймаут не задан явно — берём рекомендацию пакета (маршрут
            // значимости из handoff_create): Critical-эпик в дефолтные
            // 30 минут адаптера не влезает.
            hcfg.timeout_secs = t.clamp(MIN_HARNESS_TIMEOUT_SECS, 7200);
            let _ = writeln!(
                note,
                "timeout_secs={} — рекомендация пакета (MANIFEST.json).",
                hcfg.timeout_secs
            );
        }
        // Фоновый прогон: инструмент возвращается немедленно, агент остаётся
        // доступным пользователю (длинные handoff-прогоны не блокируют диалог).
        if args.get("background").and_then(Value::as_bool) == Some(true) {
            return Ok(self.launch_background(name, hcfg, repo, task, &note, ctx));
        }
        Ok(execute_run(name, &hcfg, &repo, &task, note).await)
    }
}

/// Синхронный прогон харнесса и форматирование результата: итог (код
/// возврата/прерывание/авто-коммит), механический разбор JSON-контракта,
/// stdout/stderr. Общий код синхронного пути (`harness_run`) и фонового
/// (`background=true` — вызывается из spawned-задачи).
async fn execute_run(
    name: &str,
    hcfg: &CodingHarnessConfig,
    repo: &Path,
    task: &str,
    note: String,
) -> ToolOutput {
    match run_harness(name, hcfg, repo, task).await {
        Ok(run) => {
            let code = run.exit_code.map_or("сигнал".into(), |c| c.to_string());
            let mut content = note;
            match run.termination {
                Termination::Completed => {
                    let _ = writeln!(
                        content,
                        "Харнесс '{name}' завершился: код {code}, {:.1} с.",
                        run.duration_secs
                    );
                }
                Termination::AbsoluteTimeout => {
                    let _ = writeln!(
                        content,
                        "Харнесс '{name}' ПРЕРВАН по абсолютному таймауту {} с \
                             (проработал {:.1} с). Процессная группа завершена \
                             (TERM→KILL), осиротевших процессов нет. Вывод ниже — \
                             частичный. Репозиторий может быть в промежуточном \
                             состоянии: перед повторным запуском проверьте git status/diff. \
                             Если задача объективно длинная — перезапустите с большим \
                             timeout_secs (до 7200) или разбейте её.",
                        hcfg.timeout_secs, run.duration_secs
                    );
                }
                Termination::IdleTimeout => {
                    let _ = writeln!(
                        content,
                        "Харнесс '{name}' ПРЕРВАН по таймауту тишины {} с: нет вывода и \
                             изменений файлов репозитория — процесс, вероятно, завис \
                             (например, ждал интерактивного ввода; для claude-code обязателен \
                             --dangerously-skip-permissions). Процессная группа завершена \
                             (TERM→KILL), сирот нет. Вывод ниже — частичный; перед \
                             повторным запуском проверьте git status/diff.",
                        hcfg.idle_timeout_secs
                    );
                }
                Termination::Canceled => {
                    let _ = writeln!(
                        content,
                        "Харнесс '{name}' ОТМЕНЁН по сигналу control-канала флота \
                             (Kill). Процессная группа завершена, сирот нет."
                    );
                }
            }
            if let Some(ac) = &run.auto_commit {
                let _ = writeln!(
                    content,
                    "АВТО-КОММИТ: исполнитель не зафиксировал результат — \
                         харнесс закоммитил {} путей: {} «{}». \
                         Контракт TASK.md требует финального коммита от самого \
                         исполнителя; при повторении проверьте задачу/доступ к git.",
                    ac.files, ac.hash, ac.message
                );
            }
            match &run.contract {
                ContractParse::Valid(c) => {
                    let _ = writeln!(
                        content,
                        "Контракт результата: status={}; assumptions: {}; \
                             open_questions: {}; conflicts: {}.",
                        c.status.as_str(),
                        c.assumptions.len(),
                        c.open_questions.len(),
                        c.conflicts.len(),
                    );
                    if c.status == ContractStatus::Blocked {
                        content.push_str(
                            "СТАТУС blocked: интеграция невозможна — сначала разберите \
                                 причины (open_questions/assumptions ниже) с архитектором.\n",
                        );
                    }
                    if !c.conflicts.is_empty() {
                        content.push_str(
                                "КОНФЛИКТЫ со spine/ADR (ОСТАНАВЛИВАЮТ интеграцию до решения архитектора):\n",
                            );
                        for conflict in &c.conflicts {
                            let _ = writeln!(content, "- {conflict}");
                        }
                    }
                    if !c.open_questions.is_empty() {
                        content.push_str("Открытые вопросы к архитектору:\n");
                        for q in &c.open_questions {
                            let _ = writeln!(content, "- {q}");
                        }
                    }
                }
                ContractParse::Invalid(reason) => {
                    let _ = writeln!(
                        content,
                        "ВНИМАНИЕ: JSON-контракт найден, но НЕВАЛИДЕН по схеме: {reason}. \
                             Машинная приёмка невозможна — перезапустите с напоминанием \
                             о схеме контракта (status из complete|partial|blocked, списки — массивы)."
                    );
                }
                ContractParse::Missing => {
                    content.push_str(
                        "ВНИМАНИЕ: JSON-контракт результата (```json с полем status) \
                             в stdout не найден — ответ может быть неполным; при необходимости \
                             перезапустите с напоминанием о контракте.\n",
                    );
                }
            }
            content.push_str("--- stdout ---\n");
            content.push_str(run.stdout.trim_end());
            if !run.stderr.trim().is_empty() {
                content.push_str("\n--- stderr ---\n");
                content.push_str(run.stderr.trim_end());
            }
            let is_error = run.exit_code != Some(0) || run.termination != Termination::Completed;
            ToolOutput {
                content,
                is_error,
                images: Vec::new(),
            }
            .truncated(HARNESS_RUN_MAX_CHARS)
        }
        Err(e) => ToolOutput::err(format!("harness_run: {e}")),
    }
}

/// Инструменты домена: `handoff_create`, `harness_run`.
#[must_use]
pub fn tools(cfg: &Config) -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(HandoffCreateTool { cfg: cfg.clone() }),
        Arc::new(HarnessRunTool { cfg: cfg.clone() }),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Конфиг с assets внутри временного каталога (изоляция от ~/.arch-ml).
    fn cfg_in(dir: &Path) -> Config {
        let mut cfg = Config::default();
        cfg.paths.assets_dir = dir.join("assets");
        cfg
    }

    fn write_file(path: &Path, text: &str) {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("mkdir");
        }
        std::fs::write(path, text).expect("write");
    }

    const SPINE: &str = "# Spine\n\n\
        ## AD-1: Единый стек\n\n\
        **Binds:** все сервисы — Rust 1.85.\n\n\
        **Prevents:** зоопарк языков в контуре.\n\n\
        **Rule:** в CI закреплён toolchain 1.85.\n\n\
        ## Прочее\n\n\
        Абзац один.\n\n\
        Абзац два.\n\n\
        Абзац три — не должен попасть в контекст.\n";

    #[test]
    fn generates_full_packet_and_preserves_user_files() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        // Маркер Rust-стека: дефолтные CONSTRAINTS — cargo-правила.
        write_file(&repo.join("Cargo.toml"), "[package]\nname = \"demo\"\n");
        let cfg = cfg_in(tmp.path());
        write_file(
            &cfg.paths.rubrics_dir().join("handoff_quality.yaml"),
            "# якорная рубрика\n",
        );
        let spine = tmp.path().join("specs/spine.md");
        write_file(&spine, SPINE);
        let adr = tmp.path().join("specs/adr/ADR-001.md");
        write_file(&adr, "# ADR-001\n\nСтатус: Accepted.\n");
        let notes = tmp.path().join("specs/notes.md");
        write_file(&notes, "# Заметки\n\nпервый\n\nвторой\n\nтретий\n");

        let packet = generate_handoff(
            &repo,
            "сделать фичу X",
            &[spine.clone(), adr.clone(), notes.clone()],
            &cfg,
            None,
            Route::Standard,
        )
        .expect("handoff");
        let dir = repo.join(".arch-handoff");
        assert_eq!(packet.dir, dir);

        let task_md = std::fs::read_to_string(dir.join("TASK.md")).expect("TASK.md");
        assert!(task_md.contains("сделать фичу X"));
        // Финализация: контракт требует git-коммита результата (иначе
        // оркестратор работу не увидит — регрессия «агенты без коммита»).
        assert!(task_md.contains("## Финализация (обязательно)"));
        assert!(task_md.contains("git add -A -- . ':!.arch-handoff'"));
        // План отката с якорем baseline (рубрика handoff_quality::rollback_plan).
        assert!(task_md.contains("## План отката"));
        let baseline = packet.baseline.as_deref().expect("baseline-якорь");
        assert!(
            task_md.contains(&format!("git reset --hard {baseline}")),
            "план отката с якорем:\n{task_md}"
        );
        assert!(task_md.contains("Владелец решения об откате"));
        assert!(
            packet.git_initialized,
            "не-git каталог — предгейт делает init"
        );
        assert!(task_md.contains("## Контракт результата"));
        assert!(task_md.contains("\"complete|partial|blocked\""));

        // MANIFEST несёт маршрут и рекомендованный таймаут (подхват harness_run).
        let manifest: Value = serde_json::from_str(
            &std::fs::read_to_string(dir.join("MANIFEST.json")).expect("MANIFEST.json"),
        )
        .expect("manifest json");
        assert_eq!(manifest["route"], "Standard");
        assert_eq!(manifest["recommended_timeout_secs"], 3600);
        assert_eq!(recommended_timeout_secs(&repo), Some(3600));

        let arch = std::fs::read_to_string(dir.join("ARCHITECTURE.md")).expect("ARCHITECTURE.md");
        // ADR-блок включён целиком (все три поля на месте).
        for field in ["**Binds:**", "**Prevents:**", "**Rule:**"] {
            assert!(arch.contains(field), "нет поля {field}");
        }
        // Прочие секции — заголовок + первые абзацы; спека мелкая, поэтому
        // сработала адаптивная глубина (окно рубрики 800–1500 токенов): все
        // три абзаца включены.
        assert!(arch.contains("Абзац два."));
        assert!(arch.contains("Абзац три"), "глубокий рендер:\n{arch}");
        assert!(arch.contains("Источники:"));

        // CONSTRAINTS.yaml создан с дефолтными правилами.
        let constraints = dir.join("CONSTRAINTS.yaml");
        let c = std::fs::read_to_string(&constraints).expect("CONSTRAINTS.yaml");
        for marker in [
            "must_not_contain",
            "unwrap",
            "dbg!",
            "file_exists",
            "command_succeeds",
            "cargo check",
            "timeout_secs: 120",
        ] {
            assert!(c.contains(marker), "CONSTRAINTS.yaml: нет '{marker}'");
        }

        // RUBRIC.yaml — копия якорной рубрики.
        let rubric = std::fs::read_to_string(dir.join("RUBRIC.yaml")).expect("RUBRIC.yaml");
        assert_eq!(rubric, "# якорная рубрика\n");

        // MANIFEST.json — мета пакета.
        let manifest_text =
            std::fs::read_to_string(dir.join("MANIFEST.json")).expect("MANIFEST.json");
        let manifest: Value = serde_json::from_str(&manifest_text).expect("manifest json");
        assert_eq!(manifest["task"], "сделать фичу X");
        assert!(manifest["created_at"].is_string());
        assert_eq!(manifest["sources"].as_array().expect("sources").len(), 3);
        let chars = manifest["epic_context_chars"].as_u64().expect("chars") as usize;
        assert_eq!(chars, arch.chars().count());
        let tokens = manifest["epic_context_tokens"].as_u64().expect("tokens") as usize;
        assert_eq!(tokens, chars / 4);
        assert_eq!(packet.epic_context_tokens, tokens);

        // adr/ — копия ADR-файла.
        assert!(dir.join("adr/ADR-001.md").is_file());
        assert!(packet.files.contains(&dir.join("adr/ADR-001.md")));

        // Повторный прогон: пользовательские CONSTRAINTS/RUBRIC не затираются,
        // TASK.md и MANIFEST.json перезаписываются.
        std::fs::write(&constraints, "# пользовательские правила\n").expect("custom constraints");
        std::fs::write(dir.join("RUBRIC.yaml"), "# пользовательская рубрика\n")
            .expect("custom rubric");
        let packet2 = generate_handoff(
            &repo,
            "другая задача",
            &[spine, adr, notes],
            &cfg,
            None,
            Route::Standard,
        )
        .expect("second handoff");
        assert_eq!(
            std::fs::read_to_string(&constraints).expect("constraints after"),
            "# пользовательские правила\n"
        );
        assert_eq!(
            std::fs::read_to_string(dir.join("RUBRIC.yaml")).expect("rubric after"),
            "# пользовательская рубрика\n"
        );
        assert!(
            std::fs::read_to_string(dir.join("TASK.md"))
                .expect("TASK.md after")
                .contains("другая задача")
        );
        assert!(packet2.files.contains(&constraints));
    }

    #[test]
    fn handoff_includes_spec_template_and_preserves_filled_spec() {
        // SPEC.md — шаблон верифицируемых контрактов интерфейсов (модель
        // «5.2»: контракты вместо прозы ARCHITECTURE.md компонента); пишется
        // один раз и не затирается повторной генерацией, как CONSTRAINTS.yaml.
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());

        let packet =
            generate_handoff(&repo, "задача", &[], &cfg, None, Route::Fast).expect("handoff");
        let spec_path = packet.dir.join("SPEC.md");
        assert!(packet.files.contains(&spec_path), "{:?}", packet.files);
        let spec = std::fs::read_to_string(&spec_path).expect("SPEC.md");
        for section in [
            "## Входы (контракты соседей)",
            "## Выходы (публикуемые контракты)",
            "## Структуры данных",
            "## Границы ошибок",
            "## Критерии верификации (тесты)",
        ] {
            assert!(spec.contains(section), "нет секции «{section}»:\n{spec}");
        }
        // EARS-подсказка на месте.
        assert!(
            spec.contains("When <событие>, the <система> shall"),
            "{spec}"
        );
        // TASK.md несёт пункт чеклиста про SPEC.md.
        let task_md = std::fs::read_to_string(packet.dir.join("TASK.md")).expect("TASK.md");
        assert!(task_md.contains("SPEC.md"), "{task_md}");

        // Заполненный SPEC.md повторная генерация не затирает.
        std::fs::write(&spec_path, "# SPEC\n\nЗаполнено архитектором.\n").expect("fill spec");
        let packet2 =
            generate_handoff(&repo, "задача 2", &[], &cfg, None, Route::Fast).expect("handoff 2");
        assert_eq!(
            std::fs::read_to_string(&spec_path).expect("SPEC.md after"),
            "# SPEC\n\nЗаполнено архитектором.\n"
        );
        assert!(packet2.files.contains(&spec_path));
    }

    #[test]
    fn handoff_fills_spec_from_passed_spec_files() {
        // Кейс 2026-09-01: исполнители (theseus, codewhale) спотыкались о
        // пустой SPEC.md-шаблон, когда архитектор передал контракты через
        // --spec: теперь SPEC.md собирается из полных текстов переданных спек.
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(repo.join("docs")).expect("mkdir");
        let cfg = cfg_in(tmp.path());
        let spec_src = repo.join("docs/SPEC-meta.md");
        std::fs::write(
            &spec_src,
            "# Спека агента\n\n## Выходы\n\n- карточка ТчВ, идемпотентность по id\n",
        )
        .expect("write spec");
        let packet = generate_handoff(
            &repo,
            "задача",
            std::slice::from_ref(&spec_src),
            &cfg,
            None,
            Route::Fast,
        )
        .expect("handoff");
        let spec = std::fs::read_to_string(packet.dir.join("SPEC.md")).expect("SPEC.md");
        assert!(spec.contains("Источник:"), "{spec}");
        assert!(
            spec.contains("карточка ТчВ, идемпотентность по id"),
            "{spec}"
        );
        assert!(
            !spec.contains("<что компонент потребляет"),
            "шаблон не нужен: {spec}"
        );
        // Правка архитектора поверх собранного файла не затирается.
        let spec_path = packet.dir.join("SPEC.md");
        std::fs::write(&spec_path, "# SPEC\n\nУточнено архитектором.\n").expect("edit");
        generate_handoff(
            &repo,
            "задача 2",
            std::slice::from_ref(&spec_src),
            &cfg,
            None,
            Route::Fast,
        )
        .expect("handoff 2");
        assert_eq!(
            std::fs::read_to_string(&spec_path).expect("SPEC.md after"),
            "# SPEC\n\nУточнено архитектором.\n"
        );
    }

    /// Модель с QAS в `<repo>/model/` для тестов критериев приёмки (ADR-007).
    fn repo_with_qas_model(repo: &Path) {
        write_file(
            &repo.join("model/NFR-001-lat.md"),
            "---\nid: NFR-001\ntype: nfr\ntitle: Latency\nstatus: accepted\nverification: hist\n---\n\np99 < 2s.\n",
        );
        write_file(
            &repo.join("model/QAS-001-peak.md"),
            "---\nid: QAS-001\ntype: qas\ntitle: Пиковая нагрузка\nstatus: accepted\n\
             implements: [NFR-001]\nsource: клиент канала\nstimulus: запрос авторизации в пике 5000 TPS\n\
             artifact: CMP-003 Authorization\nresponse: ответ об авторизации возвращён\n\
             measure: p99 < 2000 мс (NFR-001)\n---\n\nПроза.\n",
        );
    }

    #[test]
    fn handoff_unfolds_qas_into_acceptance_criteria() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        repo_with_qas_model(&repo);
        let cfg = cfg_in(tmp.path());

        generate_handoff(&repo, "задача", &[], &cfg, None, Route::Fast).expect("handoff");
        let task_md = std::fs::read_to_string(repo.join(".arch-handoff/TASK.md")).expect("TASK.md");
        // Секция появилась автоматически, без ручного копирования (DoD P1-1).
        assert!(
            task_md.contains("## Критерии приёмки (QAS из модели)"),
            "{task_md}"
        );
        assert!(task_md.contains("QAS-001"), "{task_md}");
        assert!(
            task_md.contains("запрос авторизации в пике 5000 TPS"),
            "{task_md}"
        );
        assert!(task_md.contains("p99 < 2000 мс (NFR-001)"), "{task_md}");
        // Секция стоит после задачи и до плана отката.
        let task_pos = task_md.find("задача").expect("задача");
        let qas_pos = task_md.find("## Критерии приёмки").expect("секция");
        let rollback_pos = task_md.find("## План отката").expect("откат");
        assert!(task_pos < qas_pos && qas_pos < rollback_pos, "{task_md}");
    }

    #[test]
    fn handoff_without_model_or_qas_has_no_acceptance_section() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());
        generate_handoff(&repo, "задача", &[], &cfg, None, Route::Fast).expect("handoff");
        let task_md = std::fs::read_to_string(repo.join(".arch-handoff/TASK.md")).expect("TASK.md");
        assert!(!task_md.contains("Критерии приёмки (QAS"), "{task_md}");

        // Модель есть, но QAS в ней нет — секции тоже нет.
        let repo2 = tmp.path().join("repo2");
        std::fs::create_dir_all(&repo2).expect("mkdir repo2");
        write_file(
            &repo2.join("model/CMP-001-x.md"),
            "---\nid: CMP-001\ntype: cmp\ntitle: X\nstatus: designed\n---\n",
        );
        generate_handoff(&repo2, "задача", &[], &cfg, None, Route::Fast).expect("handoff 2");
        let task_md2 =
            std::fs::read_to_string(repo2.join(".arch-handoff/TASK.md")).expect("TASK.md 2");
        assert!(!task_md2.contains("Критерии приёмки (QAS"), "{task_md2}");
    }

    #[test]
    fn handoff_with_broken_model_fails_loudly() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        write_file(&repo.join("model/broken.md"), "нет frontmatter\n");
        let cfg = cfg_in(tmp.path());
        let err = generate_handoff(&repo, "задача", &[], &cfg, None, Route::Fast)
            .expect_err("битая модель — ошибка, не молчаливый пропуск");
        assert!(err.to_string().contains("QAS"), "{err}");
    }

    #[test]
    fn handoff_git_pregate_is_idempotent() {
        // Предгейт: не-git каталог получает git init + пустой baseline-якорь;
        // повторная генерация якорь не двигает (HEAD — тот же коммит).
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());

        let p1 =
            generate_handoff(&repo, "задача", &[], &cfg, None, Route::Fast).expect("handoff 1");
        assert!(p1.git_initialized);
        let b1 = p1.baseline.clone().expect("baseline 1");
        assert_eq!(recommended_timeout_secs(&repo), Some(1800), "fast → 1800");

        let p2 =
            generate_handoff(&repo, "задача 2", &[], &cfg, None, Route::Fast).expect("handoff 2");
        assert!(!p2.git_initialized, "повторный init не нужен");
        assert_eq!(p2.baseline.as_deref(), Some(b1.as_str()), "якорь стабилен");
        // Baseline — пустой коммит, содержимое каталога не подмётено.
        let count = git_out(&repo, &["log", "--oneline"]).expect("git log");
        assert_eq!(count.lines().count(), 1, "{count}");
    }

    #[test]
    fn handoff_explicit_rollback_and_critical_route() {
        // Явный план отката попадает в TASK.md дословно; маршрут Critical
        // даёт рекомендованный таймаут 7200 (регрессия: Critical-прогон
        // обрывался на дефолтных 30 минутах адаптера). Critical требует
        // epic-context в окне рубрики — даём объёмную спеку.
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());
        let mut big_text = String::from("# Спека миграции\n\n");
        for i in 0..60 {
            let _ = write!(
                big_text,
                "## Блок {i}\n\nИнвариант: AD-{i} — дословное правило интеграции, \
                 проверяемое тестом; детали, стыки и запреты для полноты контекста.\n\n"
            );
        }
        let spec = tmp.path().join("spec-big.md");
        write_file(&spec, &big_text);

        let packet = generate_handoff(
            &repo,
            "миграция ядра",
            &[spec],
            &cfg,
            Some("Шаг 1: вернуть флаг фичи. Шаг 2: restore из snapshot БД."),
            Route::Critical,
        )
        .expect("handoff");
        assert_eq!(packet.recommended_timeout_secs, 7200);
        assert_eq!(recommended_timeout_secs(&repo), Some(7200));
        let task_md = std::fs::read_to_string(packet.dir.join("TASK.md")).expect("TASK.md");
        assert!(task_md.contains("## План отката"));
        assert!(
            task_md.contains("Шаг 1: вернуть флаг фичи. Шаг 2: restore из snapshot БД."),
            "явный откат дословно:\n{task_md}"
        );
        // Автотекст не подмешивается к явному плану.
        assert!(!task_md.contains("Сигналы отката"));
        // Несуществующий пакет — None (адаптер берёт свой дефолт).
        assert_eq!(recommended_timeout_secs(tmp.path()), None);
    }

    #[test]
    fn critical_route_refuses_thin_epic_context() {
        // Разрыв P2: для Critical контроль нижней границы окна рубрики —
        // отказ на сборке пакета, а не молчаливое предупреждение.
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());
        let err = generate_handoff(&repo, "миграция ядра", &[], &cfg, None, Route::Critical)
            .expect_err("Critical без спек обязан отказывать");
        let msg = err.to_string();
        assert!(msg.contains("ниже окна рубрики"), "{msg}");
        assert!(msg.contains("spec"), "{msg}");
        // Fast/Standard на том же объёме — собираются (Fast молча, Standard с warning).
        generate_handoff(&repo, "фикс", &[], &cfg, None, Route::Fast).expect("Fast ок");
        generate_handoff(&repo, "фикс", &[], &cfg, None, Route::Standard).expect("Standard ок");
    }

    /// Объёмная спека для Critical (epic-context в окне рубрики).
    fn big_spec(tmp: &Path) -> PathBuf {
        let mut big_text = String::from("# Спека миграции\n\n");
        for i in 0..60 {
            let _ = write!(
                big_text,
                "## Блок {i}\n\nИнвариант: AD-{i} — дословное правило интеграции, \
                 проверяемое тестом; детали, стыки и запреты для полноты контекста.\n\n"
            );
        }
        let spec = tmp.join("spec-big.md");
        write_file(&spec, &big_text);
        spec
    }

    #[test]
    fn handoff_generates_machine_readable_rollback_plan() {
        // ROLLBACK.yaml — машиночитаемый план отката (репетиция на гейте A4):
        // baseline-якорь + шаги по умолчанию, парсится rehearsal::load_plan;
        // MANIFEST.json несёт baseline_commit и rollback_plan; правки
        // архитектора повторная генерация не затирает (как CONSTRAINTS.yaml).
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());

        let packet =
            generate_handoff(&repo, "задача", &[], &cfg, None, Route::Fast).expect("handoff");
        let plan_path = packet.dir.join(crate::rehearsal::ROLLBACK_FILE);
        assert!(packet.files.contains(&plan_path), "{:?}", packet.files);
        let baseline = packet.baseline.clone().expect("baseline");
        let plan = crate::rehearsal::load_plan(&packet.dir).expect("план парсится");
        assert_eq!(plan.baseline_commit, baseline);
        assert_eq!(plan.steps.len(), 2, "якорь + откат: {:?}", plan.steps);
        assert!(plan.steps[1].run.contains("git reset --hard"));
        assert!(plan.verify.is_some(), "verify чистоты дерева");

        let manifest: Value = serde_json::from_str(
            &std::fs::read_to_string(packet.dir.join("MANIFEST.json")).expect("MANIFEST.json"),
        )
        .expect("manifest json");
        assert_eq!(manifest["baseline_commit"], baseline.as_str());
        assert!(
            manifest["rollback_plan"]
                .as_str()
                .expect("rollback_plan")
                .contains("Сигналы отката"),
            "дефолтный план в манифесте"
        );

        // Правка архитектора не затирается повторной генерацией.
        std::fs::write(
            &plan_path,
            "baseline_commit: \"\"\nsteps: []\n# пользовательский план\n",
        )
        .expect("custom plan");
        generate_handoff(&repo, "задача 2", &[], &cfg, None, Route::Fast).expect("handoff 2");
        assert!(
            std::fs::read_to_string(&plan_path)
                .expect("plan after")
                .contains("пользовательский план")
        );
    }

    #[test]
    fn critical_route_requires_baseline_and_rollback_plan() {
        // Rollback-first для Critical: пустой явный план отката — ошибка
        // сборки пакета (репетиции на A4 нечего прогонять).
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());
        let spec = big_spec(tmp.path());
        let err = generate_handoff(
            &repo,
            "миграция",
            std::slice::from_ref(&spec),
            &cfg,
            Some("   "),
            Route::Critical,
        )
        .expect_err("Critical с пустым rollback обязан отказывать");
        assert!(err.to_string().contains("rollback"), "{err}");
        // Дефолтный план (откат на baseline) — валиден: baseline от предгейта.
        let packet =
            generate_handoff(&repo, "миграция", &[spec], &cfg, None, Route::Critical).expect("ok");
        assert!(packet.baseline.is_some());
        // Для Fast пустой явный план — не ошибка маршрута (пакет собирается).
        let repo2 = tmp.path().join("repo2");
        std::fs::create_dir_all(&repo2).expect("mkdir repo2");
        generate_handoff(&repo2, "фикс", &[], &cfg, Some(""), Route::Fast).expect("Fast ок");
    }

    #[test]
    fn handoff_warns_on_dirty_tracked_tree() {
        // Хвост предгейта: грязные ОТСЛЕЖИВАЕМЫЕ файлы — откат на baseline
        // их потеряет; предупреждаем при генерации пакета.
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        git_repo_with_baseline(&repo);
        let cfg = cfg_in(tmp.path());
        let p = generate_handoff(&repo, "задача", &[], &cfg, None, Route::Fast).expect("handoff");
        assert!(!p.git_dirty_tracked, "чистое дерево — без предупреждения");
        // Модифицируем отслеживаемый файл без коммита.
        std::fs::write(repo.join("README.md"), "# изменено\n").expect("edit");
        let p = generate_handoff(&repo, "задача", &[], &cfg, None, Route::Fast).expect("handoff");
        assert!(p.git_dirty_tracked, "грязное дерево — флаг выставлен");
        // Untracked-файлы грязью не считаются (reset --hard их не трогает).
        let p3_repo = tmp.path().join("repo3");
        git_repo_with_baseline(&p3_repo);
        std::fs::write(p3_repo.join("new-file.py"), "x = 1\n").expect("untracked");
        let p3 =
            generate_handoff(&p3_repo, "задача", &[], &cfg, None, Route::Fast).expect("handoff 3");
        assert!(!p3.git_dirty_tracked, "untracked — не грязь");
    }

    #[test]
    fn long_spec_is_truncated_with_notice() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());
        let big = tmp.path().join("big.md");
        let mut text = String::from("# Большая спека\n\n");
        for i in 0..500 {
            let _ = write!(
                text,
                "## Секция {i}\n\nДостаточно длинный абзац, чтобы набрать объём контекста.\n\n"
            );
        }
        write_file(&big, &text);

        let packet = generate_handoff(&repo, "задача", &[big], &cfg, None, Route::Standard)
            .expect("handoff");
        let arch = std::fs::read_to_string(packet.dir.join("ARCHITECTURE.md")).expect("arch");
        assert!(
            arch.chars().count() <= EPIC_CONTEXT_MAX_CHARS,
            "len = {}",
            arch.chars().count()
        );
        assert!(arch.contains("Контекст усечён"));
    }

    #[test]
    fn default_constraints_follow_repo_stack() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir");

        // Пустой репозиторий — общий минимум.
        let generic = default_constraints(&repo);
        assert!(generic.contains("readme-exists"), "{generic}");
        assert!(generic.contains("Стек: generic"), "{generic}");
        assert!(!generic.contains("cargo check"), "{generic}");

        write_file(&repo.join("requirements.txt"), "pytest\n");
        let py = default_constraints(&repo);
        assert!(py.contains("pytest -q"), "{py}");
        assert!(py.contains("print\\("), "{py}");
        assert!(!py.contains("cargo check"), "{py}");

        std::fs::remove_file(repo.join("requirements.txt")).expect("rm");
        write_file(&repo.join("go.mod"), "module demo\n");
        let go = default_constraints(&repo);
        assert!(go.contains("go build ./..."), "{go}");

        std::fs::remove_file(repo.join("go.mod")).expect("rm");
        write_file(&repo.join("package.json"), "{}\n");
        assert!(default_constraints(&repo).contains("npm test"));

        std::fs::remove_file(repo.join("package.json")).expect("rm");
        write_file(&repo.join("Cargo.toml"), "[package]\nname = \"demo\"\n");
        assert!(default_constraints(&repo).contains("cargo check"));
    }

    #[test]
    fn epic_context_deepens_below_rubric_window() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());
        // Секция с пятью абзацами: на мелкой глубине (2) контекст ниже окна
        // рубрики — рендер углубляется, хвост секции доезжает.
        let spec = tmp.path().join("spec.md");
        write_file(
            &spec,
            "# Спека\n\n## Детали\n\nпервый\n\nвторой\n\nтретий\n\nчетвёртый\n\nпятый\n",
        );
        let packet = generate_handoff(&repo, "задача", &[spec], &cfg, None, Route::Standard)
            .expect("handoff");
        let arch = std::fs::read_to_string(packet.dir.join("ARCHITECTURE.md")).expect("arch");
        assert!(
            arch.contains("пятый"),
            "глубокий рендер дотянул хвост:\n{arch}"
        );
    }

    #[tokio::test]
    async fn handoff_create_warns_when_epic_below_window() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());
        let tool = HandoffCreateTool { cfg: cfg.clone() };
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg));
        // Без спек epic-context ≈ один заголовок — ниже окна рубрики.
        let out = tool
            .call(json!({"repo": "repo", "task": "x"}), &ctx)
            .await
            .expect("call");
        assert!(out.content.contains("ниже окна рубрики"), "{}", out.content);
        assert!(
            out.content.contains("стековая заготовка"),
            "{}",
            out.content
        );
    }

    #[test]
    fn builds_argv_per_prompt_mode() {
        let cfg = |args: &[&str], mode: PromptMode| CodingHarnessConfig {
            binary: "bin".into(),
            args: args.iter().map(|s| (*s).into()).collect(),
            prompt_mode: mode,
            ..CodingHarnessConfig::default()
        };

        // Positional: задача — позиционный аргумент в конце.
        let (argv, stdin) = build_argv(&cfg(&["-p"], PromptMode::Positional), "TASK");
        assert_eq!(argv, ["-p", "TASK"]);
        assert!(stdin.is_none());

        // Flag с плейсхолдером: подстановка на место {prompt}.
        let (argv, stdin) = build_argv(
            &cfg(&["agent", "--message", "{prompt}"], PromptMode::Flag),
            "TASK",
        );
        assert_eq!(argv, ["agent", "--message", "TASK"]);
        assert!(stdin.is_none());

        // Flag без плейсхолдера: задача добавляется в конец.
        let (argv, stdin) = build_argv(&cfg(&["run", "--task"], PromptMode::Flag), "TASK");
        assert_eq!(argv, ["run", "--task", "TASK"]);
        assert!(stdin.is_none());

        // Stdin: argv без задачи, задача — в stdin.
        let (argv, stdin) = build_argv(&cfg(&["-p"], PromptMode::Stdin), "TASK");
        assert_eq!(argv, ["-p"]);
        assert_eq!(stdin.as_deref(), Some("TASK"));
    }

    #[tokio::test]
    async fn runs_stdin_harness_and_captures_output() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cfg = CodingHarnessConfig {
            binary: "cat".into(),
            prompt_mode: PromptMode::Stdin,
            timeout_secs: 30,
            ..CodingHarnessConfig::default()
        };
        let run = run_harness("test-cat", &cfg, tmp.path(), "привет, харнесс")
            .await
            .expect("run");
        assert_eq!(run.harness, "test-cat");
        assert_eq!(run.exit_code, Some(0));
        assert_eq!(run.stdout, "привет, харнесс");
        assert!(run.stderr.is_empty());
        assert!(run.duration_secs >= 0.0);
        assert_eq!(run.termination, Termination::Completed);
    }

    #[tokio::test]
    async fn missing_binary_returns_hint() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cfg = CodingHarnessConfig {
            binary: "definitely-missing-bin".into(),
            timeout_secs: 5,
            ..CodingHarnessConfig::default()
        };
        let err = run_harness("theseus", &cfg, tmp.path(), "задача")
            .await
            .expect_err("должна быть ошибка");
        let msg = err.to_string();
        assert!(msg.contains("definitely-missing-bin"), "{msg}");
        assert!(msg.contains("установите"), "{msg}");
        assert!(msg.contains("[harnesses.theseus]"), "{msg}");
    }

    #[tokio::test]
    async fn absolute_timeout_returns_partial_output() {
        // Регрессия 09-12: раньше таймаут возвращал Err без вывода — модель
        // не видела, что харнесс успел сделать.
        let tmp = tempfile::tempdir().expect("tempdir");
        let cfg = CodingHarnessConfig {
            binary: "sh".into(),
            args: vec!["-c".into(), "echo MARKER; sleep 60".into()],
            prompt_mode: PromptMode::Stdin,
            timeout_secs: 1,
            idle_timeout_secs: 0,
            ..CodingHarnessConfig::default()
        };
        let run = run_harness("slow", &cfg, tmp.path(), "задача")
            .await
            .expect("прерывание — Ok с частичным выводом");
        assert_eq!(run.termination, Termination::AbsoluteTimeout);
        assert!(run.stdout.contains("MARKER"), "stdout: {}", run.stdout);
        assert!(run.duration_secs < 30.0, "{:?}", run.duration_secs);
    }

    #[tokio::test]
    async fn idle_timeout_fires_on_silence() {
        // Молчащий и непишущий процесс убивается по idle, не дожидаясь
        // абсолютного потолка.
        let tmp = tempfile::tempdir().expect("tempdir");
        let cfg = CodingHarnessConfig {
            binary: "sleep".into(),
            args: vec!["60".into()],
            prompt_mode: PromptMode::Stdin,
            timeout_secs: 120,
            idle_timeout_secs: 2,
            ..CodingHarnessConfig::default()
        };
        let run = run_harness("silent", &cfg, tmp.path(), "задача")
            .await
            .expect("run");
        assert_eq!(run.termination, Termination::IdleTimeout);
        assert!(run.duration_secs < 30.0, "{:?}", run.duration_secs);
    }

    #[tokio::test]
    async fn file_activity_resets_idle() {
        // Молчащий, но пишущий файлы процесс (типичный кодовый харнесс)
        // НЕ считается зависшим: heartbeat по mtime репозитория.
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = CodingHarnessConfig {
            binary: "sh".into(),
            args: vec![
                "-c".into(),
                "i=0; while [ $i -lt 10 ]; do touch \"f$i\"; i=$((i+1)); sleep 1; done; echo done"
                    .into(),
            ],
            prompt_mode: PromptMode::Stdin,
            timeout_secs: 60,
            // Запас против нагрузки параллельного тест-сьюта: gap между
            // touch ~1 с при окне 8 с — флаки не будет.
            idle_timeout_secs: 8,
            ..CodingHarnessConfig::default()
        };
        let run = run_harness("writer", &cfg, &repo, "задача")
            .await
            .expect("run");
        assert_eq!(
            run.termination,
            Termination::Completed,
            "stderr: {}",
            run.stderr
        );
        assert!(run.stdout.contains("done"), "stdout: {}", run.stdout);
        assert!(repo.join("f9").is_file());
    }

    #[tokio::test]
    async fn timeout_kills_whole_process_group() {
        // Регрессия 09-12 (скриншот пользователя): таймаут убивал обёртку,
        // а дочерний процесс харнесса оставался жить сиротой. Теперь группа
        // завершается целиком (TERM → KILL по -pgid).
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = CodingHarnessConfig {
            binary: "sh".into(),
            args: vec![
                "-c".into(),
                "sleep 300 & echo $! > child.pid; sleep 300".into(),
            ],
            prompt_mode: PromptMode::Stdin,
            timeout_secs: 1,
            idle_timeout_secs: 0,
            ..CodingHarnessConfig::default()
        };
        let run = run_harness("spawner", &cfg, &repo, "задача")
            .await
            .expect("run");
        assert_eq!(run.termination, Termination::AbsoluteTimeout);
        let pid = std::fs::read_to_string(repo.join("child.pid")).expect("child.pid");
        let pid = pid.trim();
        // Процесс «убит» = /proc нет ИЛИ зомби (Z/X): зомби уже мёртв, его
        // просто ещё не забрал родитель. kill -0 на зомби возвращает success,
        // поэтому проверяем state, а не сам факт ответа.
        let alive = std::fs::read_to_string(format!("/proc/{pid}/stat"))
            .ok()
            .and_then(|s| {
                s.rsplit(')')
                    .next()?
                    .split_whitespace()
                    .next()
                    .map(str::to_owned)
            })
            .is_some_and(|state| !matches!(state.as_str(), "Z" | "X" | "x"));
        assert!(!alive, "дочерний процесс {pid} пережил таймаут — сирота");
    }

    /// git-репозиторий с одним baseline-коммитом (явная идентичность —
    /// на CI/в контейнерах user.name/user.email может не быть).
    fn git_repo_with_baseline(dir: &std::path::Path) {
        std::fs::create_dir_all(dir).expect("mkdir repo");
        std::fs::write(dir.join("README.md"), "# baseline\n").expect("readme");
        let git = |args: &[&str]| {
            let out = std::process::Command::new("git")
                .arg("-C")
                .arg(dir)
                .args(args)
                .output()
                .expect("git");
            assert!(out.status.success(), "git {args:?}: {:?}", out.stderr);
        };
        git(&["init", "-q"]);
        git(&["add", "README.md"]);
        git(&[
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@test",
            "commit",
            "-q",
            "-m",
            "baseline",
        ]);
    }

    /// Харнесс-заглушка: пишет код + интерпретерный мусор, НЕ коммитит
    /// (воспроизводит дефект «агенты завершились без финального коммита»).
    fn dirty_executor_cfg() -> CodingHarnessConfig {
        CodingHarnessConfig {
            binary: "sh".into(),
            args: vec![
                "-c".into(),
                "mkdir -p spinecalc __pycache__ .arch-handoff; \
                 echo 'def validate_amount(v, l): return True' > spinecalc/amount.py; \
                 echo junk > __pycache__/x.pyc; \
                 echo meta > .arch-handoff/TASK.md; \
                 echo '{\"status\": \"complete\"}'"
                    .into(),
            ],
            prompt_mode: PromptMode::Stdin,
            timeout_secs: 30,
            idle_timeout_secs: 0,
            ..CodingHarnessConfig::default()
        }
    }

    #[tokio::test]
    async fn auto_commit_commits_executor_leftovers() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        git_repo_with_baseline(&repo);
        let run = run_harness(
            "dirty",
            &dirty_executor_cfg(),
            &repo,
            "реализовать модуль amount",
        )
        .await
        .expect("run");
        assert_eq!(run.termination, Termination::Completed);
        let ac = run.auto_commit.expect("харнесс обязан до-коммитить хвост");
        assert_eq!(ac.files, 1, "только код, без мусора: {ac:?}");
        assert!(
            ac.message
                .starts_with("harness(dirty): реализовать модуль amount")
        );
        assert!(!ac.hash.is_empty());
        // В истории — baseline + авто-коммит с кодом; физически в дереве
        // остаются лишь некоммитимые служебные/мусорные каталоги.
        let status = git_out(&repo, &["status", "--porcelain"]).expect("status");
        for line in status.lines() {
            assert!(
                line.contains(".arch-handoff/") || line.contains("__pycache__/"),
                "посторонний незакоммиченный путь: {line}"
            );
        }
        let log = git_out(&repo, &["log", "--oneline"]).expect("log");
        assert_eq!(log.lines().count(), 2, "{log}");
        let committed =
            git_out(&repo, &["show", "--name-only", "--pretty=%s", "HEAD"]).expect("show");
        assert!(committed.contains("spinecalc/amount.py"), "{committed}");
        assert!(!committed.contains("__pycache__"), "{committed}");
        assert!(!committed.contains(".arch-handoff"), "{committed}");
    }

    #[tokio::test]
    async fn auto_commit_disabled_leaves_tree_dirty() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        git_repo_with_baseline(&repo);
        let cfg = CodingHarnessConfig {
            auto_commit: false,
            ..dirty_executor_cfg()
        };
        let run = run_harness("dirty-off", &cfg, &repo, "задача")
            .await
            .expect("run");
        assert_eq!(run.termination, Termination::Completed);
        assert!(run.auto_commit.is_none());
        let status = git_out(&repo, &["status", "--porcelain"]).expect("status");
        assert!(status.contains("spinecalc/"), "{status}");
    }

    #[tokio::test]
    async fn env_allow_whitelist_isolates_harness_env() {
        // Разрыв P1 «окружение протекает между харнессами»: при непустом
        // env_allow процесс стартует с чистым окружением + whitelist + env
        // адаптера; пустой список — наследование окружения (как раньше).
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir");
        let probe = |env_allow: Vec<&str>| CodingHarnessConfig {
            binary: "/bin/sh".into(),
            args: vec![
                "-c".into(),
                "echo \"H=${HOME:-EMPTY} P=${PATH:+set} E=${EXTRA:-EMPTY}\"".into(),
            ],
            prompt_mode: PromptMode::Stdin,
            timeout_secs: 30,
            idle_timeout_secs: 0,
            auto_commit: false,
            env_allow: env_allow.iter().map(|s| (*s).into()).collect(),
            env: [("EXTRA".to_string(), "yes".to_string())]
                .into_iter()
                .collect(),
        };
        // Наследование по умолчанию: HOME и PATH видны, EXTRA из env — тоже.
        let run = run_harness("probe", &probe(vec![]), &repo, "задача")
            .await
            .expect("run");
        assert!(run.stdout.contains("H=/"), "наследование: {}", run.stdout);
        assert!(run.stdout.contains("P=set E=yes"), "{}", run.stdout);
        // Whitelist без HOME: HOME у процесса пуст, PATH и EXTRA на месте.
        let run = run_harness("probe", &probe(vec!["PATH"]), &repo, "задача")
            .await
            .expect("run");
        assert!(
            run.stdout.contains("H=EMPTY P=set E=yes"),
            "изоляция: {}",
            run.stdout
        );
    }

    #[tokio::test]
    async fn auto_commit_clean_repo_is_noop() {
        // Исполнитель всё закоммитил сам (или ничего не писал) — харнесс
        // не плодит пустых коммитов.
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        git_repo_with_baseline(&repo);
        let cfg = CodingHarnessConfig {
            binary: "sh".into(),
            args: vec!["-c".into(), "echo ok".into()],
            prompt_mode: PromptMode::Stdin,
            timeout_secs: 30,
            idle_timeout_secs: 0,
            ..CodingHarnessConfig::default()
        };
        let run = run_harness("clean", &cfg, &repo, "задача")
            .await
            .expect("run");
        assert_eq!(run.termination, Termination::Completed);
        assert!(run.auto_commit.is_none(), "пустой коммит не нужен");
        let log = git_out(&repo, &["log", "--oneline"]).expect("log");
        assert_eq!(log.lines().count(), 1, "{log}");
    }

    #[tokio::test]
    async fn handoff_create_tool_reports_summary() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir repo");
        let cfg = cfg_in(tmp.path());
        let tool = HandoffCreateTool { cfg: cfg.clone() };
        assert_eq!(tool.spec().name, "handoff_create");
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg));

        // Нет обязательного аргумента.
        let out = tool.call(json!({"task": "x"}), &ctx).await.expect("call");
        assert!(out.is_error);
        assert!(out.content.contains("'repo'"));

        // Полный вызов (repo относительно cwd).
        let out = tool
            .call(json!({"repo": "repo", "task": "сделать Y"}), &ctx)
            .await
            .expect("call");
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("Handoff-пакет создан"));
        assert!(repo.join(".arch-handoff/TASK.md").is_file());
    }

    /// Конфиг с поддельным харнессом `fake` на бинаре `cat` (stdin → stdout).
    fn cfg_with_fake_harness(dir: &Path) -> Config {
        let mut cfg = cfg_in(dir);
        cfg.harnesses.insert(
            "fake".into(),
            CodingHarnessConfig {
                binary: "cat".into(),
                prompt_mode: PromptMode::Stdin,
                timeout_secs: 30,
                ..CodingHarnessConfig::default()
            },
        );
        cfg
    }

    #[test]
    fn parse_result_contract_validates_schema_mechanically() {
        // Валидный полный контракт в последнем fenced-блоке.
        let stdout = "текст\n```json\n{\"status\": \"partial\", \"assumptions\": [\"a\"], \
                      \"open_questions\": [], \"conflicts_with_prior_decisions\": []}\n```\n";
        let ContractParse::Valid(c) = parse_result_contract(stdout) else {
            panic!("контракт найден и валиден");
        };
        assert_eq!(c.status, ContractStatus::Partial);
        assert_eq!(c.assumptions, vec!["a".to_string()]);
        // Списки опциональны: дефолт — пустые.
        let ContractParse::Valid(c) =
            parse_result_contract("```json\n{\"status\": \"complete\"}\n```")
        else {
            panic!("валиден без списков");
        };
        assert_eq!(c.status, ContractStatus::Complete);
        assert!(c.open_questions.is_empty() && c.conflicts.is_empty());
        // Без поля status — не контракт.
        assert_eq!(
            parse_result_contract("```json\n{\"x\": 1}\n```"),
            ContractParse::Missing
        );
        assert_eq!(parse_result_contract("plain text"), ContractParse::Missing);
        // Битый JSON без status — промах; со status — Invalid (не молчим).
        assert_eq!(
            parse_result_contract("```json\n{oops}\n```"),
            ContractParse::Missing
        );
        let ContractParse::Invalid(reason) =
            parse_result_contract("```json\n{\"status\": \"complete\",\n```")
        else {
            panic!("битый блок со status — Invalid");
        };
        assert!(reason.contains("невалидный JSON"), "{reason}");
        // Схема: status вне перечисления — Invalid.
        let ContractParse::Invalid(reason) =
            parse_result_contract("```json\n{\"status\": \"done\"}\n```")
        else {
            panic!("status вне перечисления — Invalid");
        };
        assert!(reason.contains("done"), "{reason}");
        // Список не массивом — Invalid.
        let ContractParse::Invalid(reason) =
            parse_result_contract("```json\n{\"status\": \"complete\", \"assumptions\": {}}\n```")
        else {
            panic!("assumptions не массив — Invalid");
        };
        assert!(reason.contains("assumptions"), "{reason}");
        // Fence уронен — голый JSON в хвосте подхватывается.
        let ContractParse::Valid(c) = parse_result_contract(
            "проза ответа\n{\"status\": \"blocked\", \"open_questions\": [\"нужен доступ к КШД\"]}",
        ) else {
            panic!("голый JSON в хвосте — валидный контракт");
        };
        assert_eq!(c.status, ContractStatus::Blocked);
        assert_eq!(c.open_questions, vec!["нужен доступ к КШД".to_string()]);
        // Последний из нескольких fenced-блоков со status побеждает.
        let two = "```json\n{\"status\": \"partial\"}\n```\nпромежуток\n```json\n{\"status\": \"complete\"}\n```";
        let ContractParse::Valid(c) = parse_result_contract(two) else {
            panic!("последний блок валиден");
        };
        assert_eq!(c.status, ContractStatus::Complete);
    }

    #[tokio::test]
    async fn harness_run_tool_validates_args() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cfg = cfg_with_fake_harness(tmp.path());
        let tool = HarnessRunTool { cfg: cfg.clone() };
        assert_eq!(tool.spec().name, "harness_run");
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg));

        let out = tool.call(json!({"repo": "."}), &ctx).await.expect("call");
        assert!(
            out.is_error && out.content.contains("'harness'"),
            "{}",
            out.content
        );

        let out = tool
            .call(json!({"harness": "nope", "repo": "."}), &ctx)
            .await
            .expect("call");
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("не настроен"), "{}", out.content);
        assert!(
            out.content.contains("claude-code"),
            "список известных: {}",
            out.content
        );

        // Нет task и нет TASK.md — понятная ошибка с подсказкой.
        let out = tool
            .call(json!({"harness": "fake", "repo": "."}), &ctx)
            .await
            .expect("call");
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("handoff_create"), "{}", out.content);
    }

    #[tokio::test]
    async fn harness_run_background_registers_and_completes() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut cfg = cfg_with_fake_harness(tmp.path());
        cfg.paths.reports_dir = tmp.path().join("reports");
        let tool = HarnessRunTool { cfg: cfg.clone() };
        let registry = crate::subagent::SubagentRegistry::new();
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg))
            .with_subagents(registry.clone());

        // Фон: инструмент возвращается сразу, задача hr-* в реестре.
        let out = tool
            .call(
                json!({"harness": "fake", "repo": ".", "task": "фон", "background": true}),
                &ctx,
            )
            .await
            .expect("call");
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("hr-"), "id задачи: {}", out.content);
        assert_eq!(registry.running(), 1, "прогон числится запущенным");

        // cat завершается мгновенно — дожидаемся финиша задачи.
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        loop {
            let task = registry
                .list()
                .into_iter()
                .find(|t| t.id.starts_with("hr-"))
                .expect("задача hr-* в реестре");
            if task.status != crate::subagent::TaskStatus::Running {
                assert_eq!(
                    task.status,
                    crate::subagent::TaskStatus::Done,
                    "отчёт: {}",
                    task.report
                );
                assert!(task.report.contains("завершился"), "отчёт: {}", task.report);
                assert!(task.finished_at.is_some());
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "фоновый прогон не завершился за 10 с"
            );
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        // Полный лог лежит файлом reports/harness/<id>.log.
        let logs = std::fs::read_dir(tmp.path().join("reports/harness")).expect("logs dir");
        assert_eq!(logs.count(), 1, "один лог-файл фонового прогона");
    }

    #[tokio::test]
    async fn harness_run_background_without_registry_is_clear_error() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cfg = cfg_with_fake_harness(tmp.path());
        let tool = HarnessRunTool { cfg: cfg.clone() };
        // Без with_subagents — реестр не подключён (headless).
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg));
        let out = tool
            .call(
                json!({"harness": "fake", "repo": ".", "task": "фон", "background": true}),
                &ctx,
            )
            .await
            .expect("call");
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("реестр"), "{}", out.content);
    }

    #[tokio::test]
    async fn harness_run_hot_reloads_adapter_config() {
        // Регрессия: агент исправил [harnesses.*] в config.toml в ходе сессии,
        // а прогон шёл со снапшота конфига, загруженного при старте процесса
        // (дефолтный `-p` для hermes — «unrecognized arguments»). Теперь
        // адаптер перечитывается из файла на каждый вызов.
        let tmp = tempfile::tempdir().expect("tempdir");
        let cfg_path = tmp.path().join("config.toml");
        let write_cfg = |marker: &str| {
            std::fs::write(
                &cfg_path,
                format!(
                    "default_model = \"deepseek\"\n[harnesses.fake]\nbinary = \"sh\"\n\
                     args = [\"-c\", \"echo {marker}\"]\nprompt_mode = \"stdin\"\n\
                     timeout_secs = 30\nidle_timeout_secs = 0\nauto_commit = false\n"
                ),
            )
            .expect("write config");
        };
        write_cfg("ADAPTER_V1");
        let cfg = Config::load(Some(&cfg_path)).expect("load");
        assert_eq!(cfg.loaded_from.as_deref(), Some(cfg_path.as_path()));
        let tool = HarnessRunTool { cfg: cfg.clone() };
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(cfg));

        let out = tool
            .call(
                json!({"harness": "fake", "repo": ".", "task": "задача"}),
                &ctx,
            )
            .await
            .expect("call 1");
        assert!(out.content.contains("ADAPTER_V1"), "{}", out.content);

        // Правка файла между вызовами — без пересоздания инструмента.
        write_cfg("ADAPTER_V2");
        let out = tool
            .call(
                json!({"harness": "fake", "repo": ".", "task": "задача"}),
                &ctx,
            )
            .await
            .expect("call 2");
        assert!(out.content.contains("ADAPTER_V2"), "{}", out.content);
        assert!(!out.content.contains("ADAPTER_V1"), "{}", out.content);
    }

    #[tokio::test]
    async fn harness_run_reads_task_md_and_extracts_contract() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        write_file(
            &repo.join(".arch-handoff/TASK.md"),
            "Сделай фичу\n\n```json\n{\"status\": \"complete\", \"assumptions\": [], \
             \"open_questions\": [\"q1\"], \"conflicts_with_prior_decisions\": []}\n```\n",
        );
        let cfg = cfg_with_fake_harness(tmp.path());
        let tool = HarnessRunTool { cfg };
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(Config::default()));

        // cat вернёт TASK.md в stdout — контракт извлекается в сводку.
        let out = tool
            .call(json!({"harness": "fake", "repo": "repo"}), &ctx)
            .await
            .expect("call");
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("код 0"), "{}", out.content);
        assert!(out.content.contains("status=complete"), "{}", out.content);
        assert!(out.content.contains("open_questions: 1"), "{}", out.content);
        assert!(out.content.contains("Сделай фичу"), "{}", out.content);
    }

    #[tokio::test]
    async fn harness_run_warns_when_contract_missing() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut cfg = cfg_in(tmp.path());
        cfg.harnesses.insert(
            "fake".into(),
            CodingHarnessConfig {
                binary: "echo".into(),
                args: vec!["нет контракта".into()],
                prompt_mode: PromptMode::Stdin,
                timeout_secs: 30,
                ..CodingHarnessConfig::default()
            },
        );
        let tool = HarnessRunTool { cfg };
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(Config::default()));
        // echo не читает stdin, печатает строку без контракта, код 0.
        let out = tool
            .call(
                json!({"harness": "fake", "repo": ".", "task": "задача"}),
                &ctx,
            )
            .await
            .expect("call");
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content.contains("контракт результата"),
            "{}",
            out.content
        );
        assert!(out.content.contains("не найден"), "{}", out.content);
    }

    #[tokio::test]
    async fn harness_run_raises_tiny_timeout_to_floor() {
        // Модель оптимистично просит 30 с — поднимаем до 600 и честно
        // сообщаем об этом в сводке (ранний обрыв оставлял репо полусобранным).
        let tmp = tempfile::tempdir().expect("tempdir");
        let cfg = cfg_with_fake_harness(tmp.path());
        let tool = HarnessRunTool { cfg };
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(Config::default()));
        let out = tool
            .call(
                json!({"harness": "fake", "repo": ".", "task": "задача", "timeout_secs": 30}),
                &ctx,
            )
            .await
            .expect("call");
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("поднят до 600"), "{}", out.content);
        // Явный разумный таймаут не трогаем.
        let out = tool
            .call(
                json!({"harness": "fake", "repo": ".", "task": "задача", "timeout_secs": 900}),
                &ctx,
            )
            .await
            .expect("call");
        assert!(!out.content.contains("поднят"), "{}", out.content);
    }

    #[test]
    fn harness_run_tool_timeout_covers_longest_run() {
        // Регрессия 11-24: агентный цикл обрывал вызов на жёстких 300 с
        // (TOOL_TIMEOUT_SECS), пока адаптер ждал 1800. Таймаут инструмента
        // обязан покрывать потолок аргумента (7200) плюс запас.
        let tool = HarnessRunTool {
            cfg: Config::default(),
        };
        assert!(tool.timeout_secs() >= 7200 + 120, "{}", tool.timeout_secs());
        let mut cfg = Config::default();
        cfg.harnesses.insert(
            "long".into(),
            CodingHarnessConfig {
                binary: "true".into(),
                timeout_secs: 8000,
                ..CodingHarnessConfig::default()
            },
        );
        assert_eq!(HarnessRunTool { cfg }.timeout_secs(), 8000 + 120);
    }

    #[tokio::test]
    async fn enforce_run_worktree_disabled_by_default() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cfg = cfg_in(tmp.path());
        // require_worktree = false (дефолт) — enforcement выключен.
        let res = enforce_run_worktree(&cfg, tmp.path(), "fake")
            .await
            .expect("enforce");
        assert!(res.is_none());
    }

    #[tokio::test]
    async fn enforce_run_worktree_creates_worktree_and_copies_handoff() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        git_repo_with_baseline(&repo);
        // Handoff-пакет по контракту НЕ коммитится — в свежем worktree его нет,
        // enforcement обязан его скопировать.
        write_file(&repo.join(".arch-handoff/TASK.md"), "задача из пакета\n");
        let mut cfg = cfg_in(tmp.path());
        cfg.paths.reports_dir = tmp.path().join("reports");
        cfg.fleet.require_worktree = true;

        let (dir, run_id) = enforce_run_worktree(&cfg, &repo, "Claude Code")
            .await
            .expect("enforce")
            .expect("worktree создан");
        // Имя — kebab-case «<слаг>-<timestamp>» (слаг санитизирован).
        assert!(run_id.starts_with("claude-code-"), "{run_id}");
        assert!(dir.join("README.md").is_file(), "worktree имеет базу");
        let task = std::fs::read_to_string(dir.join(".arch-handoff/TASK.md")).expect("TASK.md");
        assert_eq!(task, "задача из пакета\n");
        // Основное дерево не тронуто: ветка worktree в списке фабрики.
        let infos = crate::worktree::list(&repo).await.expect("list");
        assert_eq!(infos.len(), 1, "{infos:?}");
        assert_eq!(infos[0].name, run_id);
    }

    #[tokio::test]
    async fn enforce_run_worktree_requires_git_repo() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut cfg = cfg_in(tmp.path());
        cfg.fleet.require_worktree = true;
        // Не git-репозиторий — понятная ошибка ДО запуска харнесса.
        let err = enforce_run_worktree(&cfg, tmp.path(), "fake")
            .await
            .expect_err("не git-репозиторий");
        assert!(err.to_string().contains("не git-репозиторий"), "{err}");
    }

    #[tokio::test]
    async fn harness_run_tool_enforces_worktree_when_required() {
        // Сквозной путь: [fleet] require_worktree = true → harness_run прогоняет
        // харнесс в worktree, основное дерево остаётся чистым, в сводке —
        // run-id и указание на гейт владельца.
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().join("repo");
        git_repo_with_baseline(&repo);
        let mut cfg = cfg_with_fake_harness(tmp.path());
        cfg.paths.reports_dir = tmp.path().join("reports");
        cfg.fleet.require_worktree = true;
        // Исполнитель пишет файл и НЕ коммитит (auto_commit выключен, чтобы
        // работа осталась незакоммиченной в worktree — main всё равно чист).
        cfg.harnesses.get_mut("fake").expect("fake").auto_commit = false;
        cfg.harnesses.insert(
            "writer".into(),
            CodingHarnessConfig {
                binary: "sh".into(),
                args: vec![
                    "-c".into(),
                    "echo код > feature.txt; echo '{\"status\": \"complete\"}'".into(),
                ],
                prompt_mode: PromptMode::Stdin,
                timeout_secs: 30,
                idle_timeout_secs: 0,
                auto_commit: false,
                ..CodingHarnessConfig::default()
            },
        );
        let tool = HarnessRunTool { cfg };
        let ctx = ToolContext::new(tmp.path().to_path_buf(), Arc::new(Config::default()));
        let out = tool
            .call(
                json!({"harness": "writer", "repo": "repo", "task": "сделать фичу"}),
                &ctx,
            )
            .await
            .expect("call");
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("run-id 'writer-"), "{}", out.content);
        assert!(
            out.content.contains("arch fleet merge"),
            "указание на гейт: {}",
            out.content
        );
        // Основное дерево чисто — работа осталась в worktree.
        let status = git_out(&repo, &["status", "--porcelain"]).expect("status");
        assert!(status.trim().is_empty(), "main не тронут: {status}");
        assert!(!repo.join("feature.txt").is_file());
        let infos = crate::worktree::list(&repo).await.expect("list");
        assert_eq!(infos.len(), 1, "{infos:?}");
        assert!(infos[0].path.join("feature.txt").is_file());
    }
}
