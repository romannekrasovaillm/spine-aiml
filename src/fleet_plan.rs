//! План флота: декларативный DAG передачи архитектуры в работу кодовым агентам.
//!
//! Паттерн оркестрации — САХАР над одним графом: [`compile`] разворачивает
//! [`PatternKind`] в узлы и рёбра ([`PlanNode`] / [`PlanEdge`]), после чего
//! движок исполняет граф волнами по топологическим уровням. Восемь паттернов
//! не становятся восемью исполнителями (ADR-042).
//!
//! Что делает компилятор:
//! - `map_reduce` — достраивает узел [`NodeRole::Integrator`] с `depends_on`
//!   на всех worker'ов;
//! - `tournament` — узел [`NodeRole::Judge`] на каждую группу;
//! - `review_pair` — узел [`NodeRole::Reviewer`] на каждого worker'а;
//! - `walking_skeleton` — узел-скелет, от которого зависят остальные;
//! - `pipeline` — цепь по `order` (или по явным `depends_on`);
//! - `ralph` — роль [`NodeRole::Ralph`] единственному узлу;
//! - `fanout` и `dag` — ничего не синтезируют (dag — супермножество).
//!
//! Топологическая сортировка — алгоритм Кана; детект цикла возвращает путь.
//! Порядок обхода детерминирован (сортировка по id) — воспроизводимость
//! прогона и журнала.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::control::Route;
use crate::error::{HarnessError, Result};
use crate::fleet_gate::{GateSpec, ReviewVerdict};

/// Каталог планов флота в репозитории (канон, ADR-042).
pub const FLEET_DIR: &str = ".arch-fleet";

/// Дефолтный потолок одновременных процессов, если план/конфиг молчат.
pub const DEFAULT_MAX_PARALLEL: usize = 8;

/// Формат файла плана.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlanFormat {
    /// TOML — канон.
    Toml,
    /// JSON — обмен (симметрично `fleet run --items-file`).
    Json,
    /// YAML — совместимость.
    Yaml,
}

impl PlanFormat {
    /// Формат по расширению файла.
    ///
    /// # Errors
    /// Расширение не распознано.
    pub fn from_path(path: &Path) -> Result<Self> {
        match path
            .extension()
            .and_then(|e| e.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref()
        {
            Some("toml") => Ok(Self::Toml),
            Some("json") => Ok(Self::Json),
            Some("yaml" | "yml") => Ok(Self::Yaml),
            other => Err(HarnessError::Fleet(format!(
                "план '{}': расширение {other:?} не поддержано (toml|json|yaml)",
                path.display()
            ))),
        }
    }

    /// Строковое имя формата.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Toml => "toml",
            Self::Json => "json",
            Self::Yaml => "yaml",
        }
    }
}

/// Паттерн оркестрации (компилируется в DAG).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PatternKind {
    /// Веер независимых узлов без рёбер (кейс 005).
    Fanout,
    /// Конвейер стадий: цепь по `order` (или явным `depends_on`).
    Pipeline,
    /// Веер + интеграционный узел на все результаты.
    MapReduce,
    /// Лучший из N: узел `judge` выбирает первый узел группы, прошедший гейты.
    Tournament,
    /// Реализатор + состязательный ревьювер (вердикт блокирует мерж).
    ReviewPair,
    /// Цикл к неизменной цели свежими сессиями (`src/ralph.rs`).
    Ralph,
    /// Canary: сквозной срез → гейт → масштабирование.
    WalkingSkeleton,
    /// Произвольный граф зависимостей (супермножество остальных).
    Dag,
}

impl PatternKind {
    /// Разбирает имя паттерна (для флага CLI `--pattern`).
    ///
    /// # Errors
    /// Имя не распознано — с перечнем допустимых.
    pub fn parse(raw: &str) -> Result<Self> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "fanout" => Ok(Self::Fanout),
            "pipeline" => Ok(Self::Pipeline),
            "map_reduce" | "map-reduce" => Ok(Self::MapReduce),
            "tournament" => Ok(Self::Tournament),
            "review_pair" | "review-pair" => Ok(Self::ReviewPair),
            "ralph" => Ok(Self::Ralph),
            "walking_skeleton" | "walking-skeleton" => Ok(Self::WalkingSkeleton),
            "dag" => Ok(Self::Dag),
            other => Err(HarnessError::Fleet(format!(
                "неизвестный паттерн '{other}' (fanout|pipeline|map_reduce|tournament|\
                 review_pair|ralph|walking_skeleton|dag)"
            ))),
        }
    }

    /// Имя паттерна (для плана и отчётов).
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Fanout => "fanout",
            Self::Pipeline => "pipeline",
            Self::MapReduce => "map_reduce",
            Self::Tournament => "tournament",
            Self::ReviewPair => "review_pair",
            Self::Ralph => "ralph",
            Self::WalkingSkeleton => "walking_skeleton",
            Self::Dag => "dag",
        }
    }
}

/// Роль узла — кто исполняет и кто судья.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NodeRole {
    /// Обычный исполнитель (кодовый харнесс).
    Worker,
    /// Первый сквозной срез (walking skeleton).
    Skeleton,
    /// Склейка результатов зависимостей + интеграционный сценарий.
    Integrator,
    /// Состязательный ревьювер: вердикт READY/NOT-READY.
    Reviewer,
    /// Детерминированный выбор победителя группы (LLM не вызывается).
    Judge,
    /// Цикл к неизменной цели (`src/ralph.rs`).
    Ralph,
}

impl NodeRole {
    /// Имя роли.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Worker => "worker",
            Self::Skeleton => "skeleton",
            Self::Integrator => "integrator",
            Self::Reviewer => "reviewer",
            Self::Judge => "judge",
            Self::Ralph => "ralph",
        }
    }

    /// Исполняется ли роль процессом харнесса (иначе — узлом-решением).
    #[must_use]
    pub const fn runs_agent(self) -> bool {
        !matches!(self, Self::Judge)
    }
}

/// Политика отказа узла.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailPolicy {
    /// Блокирует зависимые узлы (и, при `stop_on_gate_fail`, прогон).
    Block,
    /// Узел пропускается, зависимые получают пустой вход.
    Skip,
    /// Событие в журнал, зависимые идут дальше.
    Continue,
    /// Повторы с тем же worktree (накопленная работа не теряется).
    Retry {
        /// Сколько раз повторить (включая первую попытку).
        attempts: usize,
    },
}

impl FailPolicy {
    /// Имя политики.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Block => "block",
            Self::Skip => "skip",
            Self::Continue => "continue",
            Self::Retry { .. } => "retry",
        }
    }

    /// Потолок попыток (`None` — без лимита политики узла).
    #[must_use]
    pub const fn max_attempts(self) -> Option<usize> {
        match self {
            Self::Retry { attempts } => Some(if attempts == 0 { 1 } else { attempts }),
            _ => None,
        }
    }
}

impl<'de> Deserialize<'de> for FailPolicy {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let raw = serde_json::Value::deserialize(deserializer)?;
        if let Some(s) = raw.as_str() {
            return match s.trim().to_ascii_lowercase().as_str() {
                "block" => Ok(Self::Block),
                "skip" => Ok(Self::Skip),
                "continue" => Ok(Self::Continue),
                other => Err(serde::de::Error::custom(format!(
                    "on_fail: неизвестная политика '{other}' (block|skip|continue|retry)"
                ))),
            };
        }
        let attempts = raw
            .get("retry")
            .and_then(serde_json::Value::as_u64)
            .ok_or_else(|| {
                serde::de::Error::custom("on_fail: ожидалась строка или таблица { retry = N }")
            })?;
        Ok(Self::Retry {
            attempts: attempts as usize,
        })
    }
}

impl Serialize for FailPolicy {
    fn serialize<S>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        match self {
            Self::Retry { attempts } => {
                let mut map = serde_json::Map::new();
                map.insert("retry".to_string(), serde_json::Value::from(*attempts));
                serde_json::Value::Object(map).serialize(serializer)
            }
            other => serializer.serialize_str(other.as_str()),
        }
    }
}

/// Дефолт политики отказа: продолжать (ошибка фиксируется событием).
fn de_fail_continue() -> FailPolicy {
    FailPolicy::Continue
}

/// Дефолт роли узла.
fn de_role_worker() -> NodeRole {
    NodeRole::Worker
}

/// Дефолт трудоёмкости узла.
fn de_one() -> usize {
    1
}

/// Дефолт маршрута узла — Standard.
fn de_route_default() -> Route {
    Route::Standard
}

/// Разбор маршрута из строки (`fast|standard|critical`) через
/// [`Route::from_str`](std::str::FromStr).
fn de_route<'de, D>(deserializer: D) -> std::result::Result<Route, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let raw = String::deserialize(deserializer)?;
    raw.parse::<Route>().map_err(serde::de::Error::custom)
}

/// Разбор необязательного маршрута (`fast|standard|critical`).
fn de_route_opt<'de, D>(deserializer: D) -> std::result::Result<Option<Route>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let raw = Option::<String>::deserialize(deserializer)?;
    match raw {
        Some(s) => s
            .parse::<Route>()
            .map(Some)
            .map_err(serde::de::Error::custom),
        None => Ok(None),
    }
}

/// Узел плана — надмножество [`crate::router::WorkItem`].
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlanNode {
    /// Идентификатор узла (стабильный, уникальный в плане).
    pub id: String,
    /// Текст задания агенту.
    pub spec: String,
    /// Файлы-источники epic-context (пути относительно репозитория).
    #[serde(default)]
    pub spec_files: Vec<String>,
    /// Метка под-пакета (для журнала).
    #[serde(default)]
    pub package: Option<String>,
    /// Требуемые скиллы (роутинг).
    #[serde(default)]
    pub required_skills: Vec<String>,
    /// Теги предмета/домена (роутинг).
    #[serde(default)]
    pub tags: Vec<String>,
    /// Маршрут значимости (риск).
    #[serde(default = "de_route_default", deserialize_with = "de_route")]
    pub route: Route,
    /// Домен аффинити (`code`, `docs`, …).
    #[serde(default)]
    pub domain: Option<String>,
    /// Относительная трудоёмкость (порядок назначения).
    #[serde(default = "de_one")]
    pub effort: usize,
    /// Роль узла.
    #[serde(default = "de_role_worker")]
    pub role: NodeRole,
    /// Узлы-предшественники (рёбра графа).
    #[serde(default)]
    pub depends_on: Vec<String>,
    /// Группа для `tournament` (судья выбирает внутри группы).
    #[serde(default)]
    pub group: Option<String>,
    /// Гейты ПОСЛЕ узла (`None` — гейты `[defaults]`).
    #[serde(default)]
    pub gates: Option<Vec<GateSpec>>,
    /// Политика отказа.
    #[serde(default = "de_fail_continue")]
    pub on_fail: FailPolicy,
    /// Таймаут узла, секунд (`None` — из MANIFEST, затем дефолт маршрута).
    #[serde(default)]
    pub timeout_secs: Option<u64>,
    /// Ожидаемые артефакты-выходы (сенсор «produces»).
    #[serde(default)]
    pub outputs: Vec<String>,
    /// Явный агент (иначе — детерминированный роутер).
    #[serde(default)]
    pub agent: Option<String>,
    /// Потолок раундов для роли `ralph`.
    #[serde(default)]
    pub max_rounds: Option<usize>,
}

impl PlanNode {
    /// Узел-исполнитель из формулировки задачи (для `propose`).
    #[must_use]
    pub fn worker(id: impl Into<String>, spec: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            spec: spec.into(),
            spec_files: Vec::new(),
            package: None,
            required_skills: Vec::new(),
            tags: Vec::new(),
            route: Route::Standard,
            domain: Some("code".to_string()),
            effort: 1,
            role: NodeRole::Worker,
            depends_on: Vec::new(),
            group: None,
            gates: None,
            on_fail: FailPolicy::Continue,
            timeout_secs: None,
            outputs: Vec::new(),
            agent: None,
            max_rounds: None,
        }
    }
}

/// Ребро DAG (альтернатива `depends_on`).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlanEdge {
    /// Узел-источник.
    pub from: String,
    /// Узел-приёмник (зависит от `from`).
    pub to: String,
}

/// Бюджет прогона в наблюдаемых единицах (ADR-042: деньги харнессы не рапортуют).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct BudgetSpec {
    /// Потолок времени одного узла, секунд (`0` — без лимита).
    pub max_node_secs: u64,
    /// Потолок суммарных попыток прогона (`0` — без лимита).
    pub max_attempts: usize,
}

/// Политики прогона плана.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct PlanPolicy {
    /// Потолок одновременных процессов харнессов.
    pub max_parallel: usize,
    /// Отказ гейта узла останавливает расширение волн (walking skeleton).
    pub stop_on_gate_fail: bool,
    /// Уровень автономии (`R0`–`R5`).
    pub autonomy: String,
    /// Гейт мерджа (`owner` | `none`).
    pub merge_gate: String,
    /// Прогон только в изолированном worktree.
    pub require_worktree: bool,
    /// Бюджет в наблюдаемых единицах.
    pub budget: BudgetSpec,
}

impl Default for PlanPolicy {
    fn default() -> Self {
        Self {
            max_parallel: DEFAULT_MAX_PARALLEL,
            stop_on_gate_fail: false,
            autonomy: "R2".to_string(),
            merge_gate: "owner".to_string(),
            require_worktree: true,
            budget: BudgetSpec::default(),
        }
    }
}

/// Дефолты узлов (наследуются, если у узла поле не задано).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct NodeDefaults {
    /// Маршрут по умолчанию (для узлов, оставивших стандартный).
    #[serde(default, deserialize_with = "de_route_opt")]
    pub route: Option<Route>,
    /// Брать таймаут из `MANIFEST.json` пакета.
    pub timeout_from_manifest: bool,
    /// Гейты по умолчанию.
    pub gates: Vec<GateSpec>,
    /// Политика отказа по умолчанию.
    pub on_fail: Option<FailPolicy>,
}

/// Спецификация walking-skeleton узла.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkeletonSpec {
    /// Идентификатор узла-скелета.
    pub id: String,
    /// Формулировка сквозного среза.
    pub spec: String,
    /// Гейты скелета.
    #[serde(default)]
    pub gates: Vec<GateSpec>,
}

/// План флота (файл целиком).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FleetPlan {
    /// Идентификатор плана.
    pub id: String,
    /// Метка пакета/эпика (для журнала).
    #[serde(default)]
    pub package: String,
    /// Паттерн оркестрации.
    pub pattern: PatternKind,
    /// Корень репозитория (`None` — из CLI).
    #[serde(default)]
    pub repo: Option<PathBuf>,
    /// Порядок стадий для `pipeline`.
    #[serde(default)]
    pub order: Vec<String>,
    /// Спецификация скелета для `walking_skeleton`.
    #[serde(default)]
    pub skeleton: Option<SkeletonSpec>,
    /// Политики прогона.
    #[serde(default)]
    pub policy: PlanPolicy,
    /// Дефолты узлов.
    #[serde(default)]
    pub defaults: NodeDefaults,
    /// Узлы.
    #[serde(default)]
    pub nodes: Vec<PlanNode>,
    /// Явные рёбра.
    #[serde(default)]
    pub edges: Vec<PlanEdge>,
    /// Путь источника (не из файла).
    #[serde(skip)]
    pub source: PathBuf,
    /// Формат источника (не из файла).
    #[serde(skip)]
    pub format: Option<PlanFormat>,
}

/// Скомпилированный план: узлы, уровни волн, входящие рёбра, гейты.
#[derive(Debug, Clone)]
pub struct CompiledPlan {
    /// Исходный (развёрнутый) план вместе с узлами в детерминированном порядке.
    pub plan: FleetPlan,
    /// Топологические уровни: `levels[w]` — индексы узлов волны `w`.
    pub levels: Vec<Vec<usize>>,
    /// Узел → его предшественники.
    pub incoming: HashMap<String, Vec<String>>,
    /// Узел → разрешённые гейты (после наследования дефолтов).
    pub gates: HashMap<String, Vec<GateSpec>>,
    /// Диагностика компилятора (не фатальна).
    pub warnings: Vec<String>,
}

impl CompiledPlan {
    /// Узлы в детерминированном порядке (по id).
    #[must_use]
    pub fn nodes(&self) -> &[PlanNode] {
        &self.plan.nodes
    }

    /// Узел по идентификатору.
    #[must_use]
    pub fn node(&self, id: &str) -> Option<&PlanNode> {
        self.plan.nodes.iter().find(|n| n.id == id)
    }

    /// Гейты узла (пусто, если не заданы).
    #[must_use]
    pub fn gates_of(&self, id: &str) -> &[GateSpec] {
        self.gates.get(id).map_or(&[], Vec::as_slice)
    }
}

/// Ошибка/предупреждение плана (механическая диагностика).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PlanIssue {
    /// Степень: `error` (план невалиден) или `warn`.
    pub severity: PlanSeverity,
    /// Узел (если диагностика привязана к узлу).
    pub node: Option<String>,
    /// Сообщение.
    pub message: String,
}

/// Степень диагностики плана.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PlanSeverity {
    /// План невалиден — запускать нельзя.
    Error,
    /// Подозрительно, но исполнимо.
    Warn,
}

impl PlanIssue {
    fn error(message: impl Into<String>) -> Self {
        Self {
            severity: PlanSeverity::Error,
            node: None,
            message: message.into(),
        }
    }

    fn warn(message: impl Into<String>) -> Self {
        Self {
            severity: PlanSeverity::Warn,
            node: None,
            message: message.into(),
        }
    }
}

/// Читает план по расширению файла (`.toml|.json|.yaml|.yml`).
///
/// # Errors
/// Файл не читается; расширение не распознано; содержимое не парсится.
pub fn parse_plan(path: &Path) -> Result<FleetPlan> {
    let format = PlanFormat::from_path(path)?;
    let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let mut plan = parse_plan_str(&text, format)?;
    plan.source = path.to_path_buf();
    plan.format = Some(format);
    Ok(plan)
}

/// Парсит план из строки в заданном формате.
///
/// # Errors
/// Синтаксическая ошибка формата или несовместимая схема.
pub fn parse_plan_str(text: &str, format: PlanFormat) -> Result<FleetPlan> {
    let plan = match format {
        PlanFormat::Toml => toml::from_str::<FleetPlan>(text)?,
        PlanFormat::Json => serde_json::from_str::<FleetPlan>(text)?,
        PlanFormat::Yaml => serde_yaml_ng::from_str::<FleetPlan>(text)?,
    };
    Ok(plan)
}

/// Метаданные handoff-пакета, достаточные для предложения плана.
#[derive(Deserialize)]
struct ManifestMeta {
    #[serde(default)]
    task: String,
    #[serde(default)]
    route: String,
    #[serde(default)]
    recommended_timeout_secs: Option<u64>,
    #[serde(default)]
    sources: Vec<String>,
}

/// Предлагает скелет плана по handoff-пакету репозитория.
///
/// Читает `.arch-handoff/MANIFEST.json` (задача, маршрут, рекомендованный
/// таймаут, источники) и строит ЧЕРНОВИК плана с обоснованием. Декомпозиция
/// пакета на узлы — работа архитектора, а не эвристики: предложение несёт
/// один узел с формулировкой задачи пакета и рекомендуемый паттерн; узлы
/// владелец дописывает сам (ADR-042, «Spine предлагает — человек утверждает»).
///
/// Возвращает план и текст обоснования.
///
/// # Errors
/// `MANIFEST.json` не найден или нечитаем; id плана не выводится.
pub fn propose(repo: &Path, pattern: Option<PatternKind>) -> Result<(FleetPlan, String)> {
    let manifest_path = repo.join(".arch-handoff/MANIFEST.json");
    if !manifest_path.is_file() {
        return Err(HarnessError::Fleet(format!(
            "нет handoff-пакета: {} (сначала `arch-ml handoff <харнесс> --repo … --task …`)",
            manifest_path.display()
        )));
    }
    let text =
        std::fs::read_to_string(&manifest_path).map_err(|e| HarnessError::io(&manifest_path, e))?;
    let mut meta: ManifestMeta = serde_json::from_str(&text)?;
    let route = meta.route.parse::<Route>().unwrap_or(Route::Standard);

    let (pattern, why) = match pattern {
        Some(p) => (p, "паттерн задан владельцем".to_string()),
        None => match route {
            Route::Fast => (
                PatternKind::Fanout,
                "маршрут Fast: низкий риск — хватает веера исполнителей".to_string(),
            ),
            Route::Standard => (
                PatternKind::Fanout,
                "маршрут Standard: сначала веер по узлам; если узлы связаны — \
                 переключитесь на pipeline/map_reduce, дописав зависимости"
                    .to_string(),
            ),
            Route::Critical => (
                PatternKind::WalkingSkeleton,
                "маршрут Critical: сначала сквозной срез (canary), масштабирование — \
                 только после зелёного гейта скелета"
                    .to_string(),
            ),
        },
    };

    let id = {
        let stem = repo
            .file_name()
            .map_or_else(|| "fleet".to_string(), |n| n.to_string_lossy().into_owned());
        format!("{}-{}", slugify(&stem), pattern.as_str())
    };
    let sources_text = if meta.sources.is_empty() {
        "—".to_string()
    } else {
        meta.sources.join(", ")
    };
    let mut node = PlanNode::worker("node-1", meta.task.clone());
    node.route = route;
    node.spec_files = std::mem::take(&mut meta.sources);
    node.outputs = Vec::new();
    if let Some(t) = meta.recommended_timeout_secs {
        node.timeout_secs = Some(t);
    }

    let mut plan = FleetPlan {
        id: id.clone(),
        package: repo
            .file_name()
            .map_or_else(|| "fleet".to_string(), |n| n.to_string_lossy().into_owned()),
        pattern,
        repo: Some(repo.to_path_buf()),
        order: Vec::new(),
        skeleton: None,
        policy: PlanPolicy::default(),
        defaults: NodeDefaults {
            route: Some(route),
            timeout_from_manifest: true,
            gates: vec![
                GateSpec::Contract {
                    allow: vec!["complete".to_string()],
                },
                GateSpec::Fitness { constraints: None },
            ],
            on_fail: Some(FailPolicy::Block),
        },
        nodes: vec![node],
        edges: Vec::new(),
        source: PathBuf::new(),
        format: Some(PlanFormat::Toml),
    };
    if pattern == PatternKind::WalkingSkeleton {
        plan.skeleton = Some(SkeletonSpec {
            id: "node-1".to_string(),
            spec: meta.task.clone(),
            gates: plan.defaults.gates.clone(),
        });
        plan.policy.stop_on_gate_fail = true;
    }

    let rationale = format!(
        "Предложение плана '{id}':\n\
         - пакет: {} (источники: {})\n\
         - маршрут из MANIFEST: {route:?}, таймаут {} с\n\
         - паттерн: {} — {why}\n\
         - узлов в черновике: 1 (декомпозиция на узлы — работа архитектора;\n  \
           допишите [[nodes]] и зависимости, затем `fleet plan validate`)\n",
        if meta.task.is_empty() {
            "задача не записана в MANIFEST"
        } else {
            meta.task.trim()
        },
        sources_text,
        meta.recommended_timeout_secs.unwrap_or(0),
        pattern.as_str(),
    );
    Ok((plan, rationale))
}

/// Пишет план в каталог `dir` как `<id>.plan.toml` (каталог создаётся).
///
/// Обычно `dir` — это `<repo>/.arch-fleet` ([`FLEET_DIR`]); функция каталог
/// сама не достраивает, чтобы вызывающий мог указать свой.
///
/// # Errors
/// Каталог не создаётся; план не сериализуется (например, значение, которое
/// TOML не умеет записать).
pub fn write_plan(plan: &FleetPlan, dir: &Path) -> Result<PathBuf> {
    std::fs::create_dir_all(dir).map_err(|e| HarnessError::io(dir, e))?;
    let path = dir.join(format!("{}.plan.toml", slugify(&plan.id)));
    let text = toml::to_string_pretty(plan)
        .map_err(|e| HarnessError::Fleet(format!("сериализация плана: {e}")))?;
    std::fs::write(&path, text).map_err(|e| HarnessError::io(&path, e))?;
    Ok(path)
}

/// kebab-слаг из произвольной строки (для id плана и имени файла).
#[must_use]
pub fn slugify(raw: &str) -> String {
    let slug: String = raw
        .to_ascii_lowercase()
        .chars()
        .map(|c| {
            if c.is_ascii_lowercase() || c.is_ascii_digit() {
                c
            } else {
                '-'
            }
        })
        .collect();
    let trimmed = slug.trim_matches('-').to_string();
    if trimmed.is_empty() {
        "fleet".to_string()
    } else {
        trimmed
    }
}

/// Механические проверки плана без побочных эффектов.
///
/// Возвращает список диагностик: `Error` — запускать нельзя, `Warn` —
/// подозрительно. Пустой список — план валиден.
#[must_use]
pub fn validate_plan(plan: &FleetPlan) -> Vec<PlanIssue> {
    let mut issues = Vec::new();
    if plan.id.trim().is_empty() {
        issues.push(PlanIssue::error("не задан id плана"));
    }
    if plan.nodes.is_empty() {
        issues.push(PlanIssue::error(
            "в плане нет узлов ([[nodes]]): нечего исполнять",
        ));
    }
    if plan.policy.max_parallel == 0 {
        issues.push(PlanIssue::error(
            "policy.max_parallel = 0: флот не сможет запустить ни одного процесса",
        ));
    }
    match compile(plan.clone()) {
        Ok(compiled) => {
            issues.extend(compiled.warnings.iter().map(PlanIssue::warn));
            issues.extend(preflight_capacity(&compiled));
        }
        Err(e) => issues.push(PlanIssue::error(e.to_string())),
    }
    issues
}

/// Предупреждение о заведомо опасном потолке параллелизма (память, лимиты).
fn preflight_capacity(compiled: &CompiledPlan) -> Vec<PlanIssue> {
    let parallel = compiled.plan.policy.max_parallel;
    let cores = std::thread::available_parallelism().map_or(1, std::num::NonZeroUsize::get);
    let mut out = Vec::new();
    if parallel > cores * 4 {
        out.push(PlanIssue::warn(format!(
            "max_parallel = {parallel} при {cores} ядрах: процессы харнессов тяжёлые — \
             упор в память и лимиты провайдера вероятен; потолок применяется пулом"
        )));
    }
    if compiled.nodes().len() > 100 {
        out.push(PlanIssue::warn(format!(
            "узлов {}: диск под worktree-на-узел растёт линейно — после прогона \
             проверьте `arch-ml fleet audit` (prune старше 30 дней)",
            compiled.nodes().len()
        )));
    }
    out
}

/// Компилирует план: разворачивает паттерн в DAG, наследует дефолты, считает
/// топологические уровни (алгоритм Кана).
///
/// # Errors
/// Дубль id; ребро на несуществующий узел; цикл в графе (с путём);
/// паттерн несовместим с узлами (`pipeline` без `order` и без `depends_on`;
/// `walking_skeleton` без скелета; `tournament` без групп; `ralph` без узла).
pub fn compile(mut plan: FleetPlan) -> Result<CompiledPlan> {
    let mut warnings = Vec::new();

    inherit_defaults(&mut plan);

    // Все методы, сведённые к рёбрам (для цикл-детекта и волн).
    let mut by_id: BTreeMap<String, PlanNode> = BTreeMap::new();
    for node in plan.nodes.drain(..) {
        if by_id.contains_key(&node.id) {
            return Err(HarnessError::Fleet(format!(
                "дубль идентификатора узла '{}'",
                node.id
            )));
        }
        by_id.insert(node.id.clone(), node);
    }

    match plan.pattern {
        PatternKind::Fanout | PatternKind::Dag => {}
        PatternKind::Pipeline => expand_pipeline(&mut by_id, &plan)?,
        PatternKind::MapReduce => expand_map_reduce(&mut by_id, &plan, &mut warnings),
        PatternKind::Tournament => expand_tournament(&mut by_id, &mut warnings)?,
        PatternKind::ReviewPair => expand_review_pair(&mut by_id, &plan, &mut warnings),
        PatternKind::WalkingSkeleton => expand_walking_skeleton(&mut by_id, &plan, &mut warnings)?,
        PatternKind::Ralph => expand_ralph(&mut by_id)?,
    }

    // Рёбра: depends_on узлов + явные [[edges]].
    let mut incoming: HashMap<String, Vec<String>> = HashMap::new();
    for id in by_id.keys() {
        incoming.insert(id.clone(), Vec::new());
    }
    let mut add_edge = |from: &str, to: &str| -> Result<()> {
        if !by_id.contains_key(from) {
            return Err(HarnessError::Fleet(format!(
                "ребро из несуществующего узла '{from}'"
            )));
        }
        if !by_id.contains_key(to) {
            return Err(HarnessError::Fleet(format!(
                "ребро в несуществующий узел '{to}'"
            )));
        }
        if from == to {
            return Err(HarnessError::Fleet(format!(
                "узел '{from}' зависит от себя"
            )));
        }
        let list = incoming
            .get_mut(to)
            .ok_or_else(|| HarnessError::Fleet(format!("нет записи для узла '{to}'")))?;
        if !list.iter().any(|x| x == from) {
            list.push(from.to_string());
        }
        Ok(())
    };
    let explicit: Vec<PlanEdge> = plan.edges.clone();
    for (id, node) in &by_id {
        for dep in &node.depends_on {
            add_edge(dep, id)?;
        }
    }
    for edge in &explicit {
        add_edge(&edge.from, &edge.to)?;
    }
    for list in incoming.values_mut() {
        list.sort();
        list.dedup();
    }

    let levels = topo_levels(&by_id, &incoming)?;

    // Диагностика.
    for node in by_id.values() {
        if node.spec.trim().is_empty() && node.role.runs_agent() {
            warnings.push(format!("узел '{}': пустая формулировка задачи", node.id));
        }
        if node.gates.is_none() && plan.defaults.gates.is_empty() && node.role.runs_agent() {
            warnings.push(format!(
                "узел '{}': гейтов нет — результат агента ничем не проверяется",
                node.id
            ));
        }
    }
    if plan.pattern == PatternKind::Dag && explicit.is_empty() {
        warnings.push(
            "паттерн dag без [[edges]]: граф пуст, узлы пойдут одной волной — \
             для веера есть pattern = \"fanout\""
                .to_string(),
        );
    }

    let gates: HashMap<String, Vec<GateSpec>> = by_id
        .iter()
        .map(|(id, n)| (id.clone(), n.gates.clone().unwrap_or_default()))
        .collect();
    plan.nodes = by_id.into_values().collect();
    Ok(CompiledPlan {
        plan,
        levels,
        incoming,
        gates,
        warnings,
    })
}

/// Наследует дефолты плана в узлы (не заданные поля).
fn inherit_defaults(plan: &mut FleetPlan) {
    let defaults = plan.defaults.clone();
    for node in &mut plan.nodes {
        if let Some(route) = defaults.route {
            // Явно заданный маршрут не перетираем: дефолт применяется, если
            // узел нёс стандартный (сериализованный дефолт derive).
            if node.route == Route::Standard {
                node.route = route;
            }
        }
        if let Some(policy) = defaults.on_fail {
            if node.on_fail == FailPolicy::Continue {
                node.on_fail = policy;
            }
        }
        if node.gates.is_none() && !defaults.gates.is_empty() {
            node.gates = Some(defaults.gates.clone());
        }
    }
}

/// Разворачивает `pipeline`: цепь по `order`, иначе — требуются `depends_on`.
///
/// # Errors
/// Ни `order`, ни `depends_on` не заданы; `order` ссылается на несуществующий узел.
fn expand_pipeline(by_id: &mut BTreeMap<String, PlanNode>, plan: &FleetPlan) -> Result<()> {
    if plan.order.is_empty() {
        let has_deps = by_id.values().any(|n| !n.depends_on.is_empty());
        if !has_deps {
            return Err(HarnessError::Fleet(
                "pipeline: нужен order = [\"стадия1\", \"стадия2\", …] или явные depends_on"
                    .to_string(),
            ));
        }
        return Ok(());
    }
    for stage in &plan.order {
        if !by_id.contains_key(stage) {
            return Err(HarnessError::Fleet(format!(
                "pipeline: стадия '{stage}' из order не найдена среди узлов"
            )));
        }
    }
    for pair in plan.order.windows(2) {
        let (prev, next) = (&pair[0], &pair[1]);
        let node = by_id
            .get_mut(next)
            .ok_or_else(|| HarnessError::Fleet(format!("pipeline: нет узла '{next}'")))?;
        if !node.depends_on.iter().any(|d| d == prev) {
            node.depends_on.push(prev.clone());
        }
    }
    Ok(())
}

/// Достраивает узел-интегратор для `map_reduce`.
fn expand_map_reduce(
    by_id: &mut BTreeMap<String, PlanNode>,
    plan: &FleetPlan,
    warnings: &mut Vec<String>,
) {
    let workers: Vec<String> = by_id
        .values()
        .filter(|n| n.role == NodeRole::Worker)
        .map(|n| n.id.clone())
        .collect();
    if workers.is_empty() {
        warnings.push("map_reduce: нет worker-узлов — интегратору нечего склеивать".to_string());
        return;
    }
    if by_id.values().any(|n| n.role == NodeRole::Integrator) {
        return;
    }
    let mut integrator = PlanNode::worker(
        unique_id(by_id, "reduce"),
        format!(
            "Интеграция результатов узлов {}: сведи артефакты в один пакет, прогони \
             сквозной сценарий по контрактам пакета и зафиксируй evidence.",
            workers.join(", ")
        ),
    );
    integrator.role = NodeRole::Integrator;
    integrator.depends_on = workers;
    if !plan.defaults.gates.is_empty() {
        integrator.gates = Some(plan.defaults.gates.clone());
    }
    by_id.insert(integrator.id.clone(), integrator);
}

/// Достраивает узлы-судьи для `tournament`.
///
/// # Errors
/// Турнир без групп у узлов.
fn expand_tournament(
    by_id: &mut BTreeMap<String, PlanNode>,
    warnings: &mut Vec<String>,
) -> Result<()> {
    let mut groups: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for node in by_id.values() {
        if let Some(group) = &node.group {
            groups
                .entry(group.clone())
                .or_default()
                .push(node.id.clone());
        }
    }
    if groups.is_empty() {
        return Err(HarnessError::Fleet(
            "tournament: ни у одного узла не задана group — судье некого выбирать".to_string(),
        ));
    }
    for (group, mut members) in groups {
        members.sort();
        if members.len() < 2 {
            warnings.push(format!(
                "tournament: в группе '{group}' один узел — турнир вырожден"
            ));
        }
        let judge_id = unique_id(by_id, &format!("judge-{group}"));
        let mut judge = PlanNode::worker(
            judge_id,
            format!(
                "Детерминированный выбор победителя группы '{group}': берётся первый узел \
                 (в порядке id), прошедший ВСЕ гейты. LLM не вызывается."
            ),
        );
        judge.role = NodeRole::Judge;
        judge.group = Some(group);
        judge.depends_on = members;
        by_id.insert(judge.id.clone(), judge);
    }
    Ok(())
}

/// Достраивает узлы-ревьюверы для `review_pair`.
fn expand_review_pair(
    by_id: &mut BTreeMap<String, PlanNode>,
    plan: &FleetPlan,
    warnings: &mut Vec<String>,
) {
    if !plan.policy.merge_gate.eq_ignore_ascii_case("owner") {
        warnings.push(
            "review_pair при merge_gate != \"owner\": вердикт ревьювера не блокирует мерж"
                .to_string(),
        );
    }
    let already_reviewed: HashSet<String> = by_id
        .values()
        .filter(|n| n.role == NodeRole::Reviewer)
        .flat_map(|n| n.depends_on.clone())
        .collect();
    let workers: Vec<String> = by_id
        .values()
        .filter(|n| n.role == NodeRole::Worker && !already_reviewed.contains(&n.id))
        .map(|n| n.id.clone())
        .collect();
    for worker in workers {
        let node_id = unique_id(by_id, &format!("review-{worker}"));
        let mut reviewer = PlanNode::worker(node_id, reviewer_spec(&worker));
        reviewer.role = NodeRole::Reviewer;
        reviewer.depends_on = vec![worker];
        reviewer.gates = Some(vec![GateSpec::Review {
            min: ReviewVerdict::Ready,
        }]);
        by_id.insert(reviewer.id.clone(), reviewer);
    }
}

/// Формулировка задачи ревьювера (состязательная установка, ADR-042).
fn reviewer_spec(worker: &str) -> String {
    format!(
        "Ты НЕ проектировал эту систему и не писал этот код (узел '{worker}'). Твоя работа — \
         найти, что сломается: нарушения инвариантов ARCHITECTURE-SPINE.md, несоответствия \
         SPEC/CONSTRAINTS пакета, пропущенные краевые случаи, ложные тесты. Каждая находка \
         обязана опираться на проверяемое свидетельство (файл:строка), а не на вкус. \
         Заверши вывод JSON-блоком: {{\"verdict\":\"READY\"|\"NOT-READY\",\"findings\":[…]}}."
    )
}

/// Разворачивает `walking_skeleton`: узел-скелет, от которого зависят прочие.
///
/// # Errors
/// Скелет не задан ни `[skeleton]`, ни ролью узла.
fn expand_walking_skeleton(
    by_id: &mut BTreeMap<String, PlanNode>,
    plan: &FleetPlan,
    warnings: &mut Vec<String>,
) -> Result<()> {
    if !plan.policy.stop_on_gate_fail {
        warnings.push(
            "walking_skeleton при stop_on_gate_fail = false: отказ скелета не остановит \
             масштабирование — смысл canary теряется"
                .to_string(),
        );
    }
    let skeleton_id = if let Some(spec) = &plan.skeleton {
        let id = spec.id.clone();
        if let Some(existing) = by_id.get_mut(&id) {
            existing.role = NodeRole::Skeleton;
            if !spec.gates.is_empty() {
                existing.gates = Some(spec.gates.clone());
            }
        } else {
            let mut node = PlanNode::worker(id.clone(), spec.spec.clone());
            node.role = NodeRole::Skeleton;
            node.gates = Some(spec.gates.clone());
            by_id.insert(id.clone(), node);
        }
        id
    } else {
        by_id
            .values()
            .find(|n| n.role == NodeRole::Skeleton)
            .map(|n| n.id.clone())
            .ok_or_else(|| {
                HarnessError::Fleet(
                    "walking_skeleton: задайте [skeleton] или роль skeleton одному из узлов"
                        .to_string(),
                )
            })?
    };
    for node in by_id.values_mut() {
        if node.id != skeleton_id && !node.depends_on.iter().any(|d| d == &skeleton_id) {
            node.depends_on.push(skeleton_id.clone());
        }
    }
    Ok(())
}

/// Разворачивает `ralph`: единственный узел получает роль ralph.
///
/// # Errors
/// Узлов не ровно один.
fn expand_ralph(by_id: &mut BTreeMap<String, PlanNode>) -> Result<()> {
    if by_id.len() != 1 {
        return Err(HarnessError::Fleet(format!(
            "ralph: ожидался ровно один узел (цель цикла), найдено {}",
            by_id.len()
        )));
    }
    if let Some(node) = by_id.values_mut().next() {
        node.role = NodeRole::Ralph;
    }
    Ok(())
}

/// Свободный id с суффиксом `-2`, `-3`, … если базовый занят.
fn unique_id(by_id: &BTreeMap<String, PlanNode>, base: &str) -> String {
    if !by_id.contains_key(base) {
        return base.to_string();
    }
    let mut n = 2usize;
    loop {
        let candidate = format!("{base}-{n}");
        if !by_id.contains_key(&candidate) {
            return candidate;
        }
        n += 1;
    }
}

/// Топологические уровни (алгоритм Кана); порядок внутри уровня — по id.
///
/// # Errors
/// Цикл в графе — с перечислением пути.
fn topo_levels(
    by_id: &BTreeMap<String, PlanNode>,
    incoming: &HashMap<String, Vec<String>>,
) -> Result<Vec<Vec<usize>>> {
    let ids: Vec<String> = by_id.keys().cloned().collect();
    let index: HashMap<&str, usize> = ids
        .iter()
        .enumerate()
        .map(|(i, id)| (id.as_str(), i))
        .collect();
    let mut pending: HashMap<&str, HashSet<&str>> = HashMap::new();
    for id in &ids {
        let deps: HashSet<&str> = incoming
            .get(id.as_str())
            .map(|v| v.iter().map(String::as_str).collect())
            .unwrap_or_default();
        pending.insert(id.as_str(), deps);
    }
    let mut done: HashSet<&str> = HashSet::new();
    let mut levels: Vec<Vec<usize>> = Vec::new();
    while done.len() < ids.len() {
        let mut wave: Vec<&str> = pending
            .iter()
            .filter(|(id, deps)| !done.contains(*id) && deps.iter().all(|d| done.contains(d)))
            .map(|(id, _)| *id)
            .collect();
        if wave.is_empty() {
            let stuck: Vec<&str> = pending
                .keys()
                .copied()
                .filter(|id| !done.contains(id))
                .collect();
            return Err(HarnessError::Fleet(format!(
                "цикл в графе плана: {}",
                cycle_path(&stuck, &pending)
            )));
        }
        wave.sort_unstable();
        let mut idx = Vec::with_capacity(wave.len());
        for id in &wave {
            done.insert(id);
            idx.push(*index.get(id).unwrap_or(&0));
        }
        idx.sort_unstable();
        levels.push(idx);
    }
    Ok(levels)
}

/// Путь цикла среди «застрявших» узлов (для понятной ошибки).
fn cycle_path(stuck: &[&str], pending: &HashMap<&str, HashSet<&str>>) -> String {
    let set: HashSet<&str> = stuck.iter().copied().collect();
    let start = stuck.first().copied().unwrap_or("?");
    let mut path: Vec<&str> = vec![start];
    let mut seen: HashSet<&str> = HashSet::new();
    seen.insert(start);
    let mut current = start;
    loop {
        let next = pending
            .get(current)
            .and_then(|deps| deps.iter().copied().find(|d| set.contains(*d)));
        match next {
            Some(n) if n == start => {
                path.push(n);
                break;
            }
            Some(n) if seen.contains(n) => {
                path.push(n);
                break;
            }
            Some(n) => {
                path.push(n);
                seen.insert(n);
                current = n;
            }
            None => break,
        }
    }
    path.join(" → ")
}

/// Текстовая проекция плана: волны, узлы, роли, гейты, таймауты.
#[must_use]
pub fn render_plan(compiled: &CompiledPlan) -> String {
    use std::fmt::Write as _;
    let plan = &compiled.plan;
    let mut s = String::new();
    let _ = writeln!(
        s,
        "План: {} ({}) — паттерн {}, узлов {}, волн {}",
        plan.id,
        if plan.package.is_empty() {
            "без пакета"
        } else {
            &plan.package
        },
        plan.pattern.as_str(),
        compiled.nodes().len(),
        compiled.levels.len()
    );
    let _ = writeln!(
        s,
        "Политика: max_parallel={}, worktree={}, merge_gate={}, autonomy={}, stop_on_gate_fail={}",
        plan.policy.max_parallel,
        plan.policy.require_worktree,
        plan.policy.merge_gate,
        plan.policy.autonomy,
        plan.policy.stop_on_gate_fail
    );
    for (w, level) in compiled.levels.iter().enumerate() {
        let _ = writeln!(s, "\nВолна {} ({} узлов):", w + 1, level.len());
        for idx in level {
            let Some(node) = compiled.nodes().get(*idx) else {
                continue;
            };
            let deps = compiled
                .incoming
                .get(&node.id)
                .map(|d| d.join(", "))
                .unwrap_or_default();
            let gates = compiled
                .gates_of(&node.id)
                .iter()
                .map(GateSpec::name)
                .collect::<Vec<_>>()
                .join("+");
            let route = format!("{:?}", node.route).to_ascii_lowercase();
            let outputs = if node.outputs.is_empty() {
                String::new()
            } else {
                format!("outputs={}", node.outputs.join(","))
            };
            let _ = writeln!(
                s,
                "  {:<24} {:<10} route={route:<8} от={:<16} гейты={:<16} {outputs}",
                node.id,
                node.role.as_str(),
                if deps.is_empty() {
                    "—".to_string()
                } else {
                    deps
                },
                if gates.is_empty() {
                    "—".to_string()
                } else {
                    gates
                },
            );
        }
    }
    if !compiled.warnings.is_empty() {
        let _ = writeln!(s, "\nПредупреждения:");
        for w in &compiled.warnings {
            let _ = writeln!(s, "  ⚠ {w}");
        }
    }
    s
}

/// Mermaid-диаграмма графа плана.
#[must_use]
pub fn render_mermaid(compiled: &CompiledPlan) -> String {
    use std::fmt::Write as _;
    let mut s = String::from("graph TD\n");
    for node in compiled.nodes() {
        let shape = match node.role {
            NodeRole::Judge => format!("{}({{{}}})", sanitize(&node.id), node.role.as_str()),
            NodeRole::Reviewer => format!("{}[/{}/]", sanitize(&node.id), node.role.as_str()),
            NodeRole::Integrator => format!("{}[[{}]]", sanitize(&node.id), node.role.as_str()),
            _ => format!("{}[{}]", sanitize(&node.id), node.role.as_str()),
        };
        let _ = writeln!(s, "  {shape}");
    }
    for (to, froms) in &compiled.incoming {
        for from in froms {
            let _ = writeln!(s, "  {} --> {}", sanitize(from), sanitize(to));
        }
    }
    s
}

/// Mermaid-безопасный идентификатор узла.
fn sanitize(id: &str) -> String {
    let mut out = String::with_capacity(id.len());
    for ch in id.chars() {
        if ch.is_ascii_alphanumeric() || ch == '_' {
            out.push(ch);
        } else {
            out.push('_');
        }
    }
    if out.chars().next().is_some_and(|c| c.is_ascii_digit()) {
        out.insert(0, 'n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(text: &str) -> FleetPlan {
        parse_plan_str(text, PlanFormat::Toml).expect("parse")
    }

    #[test]
    fn toml_plan_parses_with_gate_shorthand() {
        let text = r#"
id = "demo"
pattern = "fanout"

[defaults]
gates = ["contract", { command = { cmd = "pytest -q", timeout_secs = 60 } }]

[[nodes]]
id = "p1"
spec = "модуль 1"
route = "critical"
"#;
        let plan = parse(text);
        assert_eq!(plan.id, "demo");
        assert_eq!(plan.nodes.len(), 1);
        assert_eq!(plan.nodes[0].route, Route::Critical);
        assert_eq!(plan.defaults.gates.len(), 2);
        assert_eq!(plan.defaults.gates[0].name(), "contract");
        assert_eq!(
            plan.defaults.gates[1],
            GateSpec::Command {
                cmd: "pytest -q".into(),
                timeout_secs: 60
            }
        );
    }

    #[test]
    fn same_plan_in_three_formats() {
        let toml_text = "id = \"x\"\npattern = \"fanout\"\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\n";
        let json_text = r#"{"id":"x","pattern":"fanout","nodes":[{"id":"a","spec":"s"}]}"#;
        let yaml_text = "id: x\npattern: fanout\nnodes:\n  - id: a\n    spec: s\n";
        let a = parse_plan_str(toml_text, PlanFormat::Toml).expect("toml");
        let b = parse_plan_str(json_text, PlanFormat::Json).expect("json");
        let c = parse_plan_str(yaml_text, PlanFormat::Yaml).expect("yaml");
        assert_eq!(a.id, b.id);
        assert_eq!(b.id, c.id);
        assert_eq!(a.pattern, PatternKind::Fanout);
        assert_eq!(a.nodes[0].id, "a");
    }

    #[test]
    fn plan_with_parametrised_gates_round_trips_through_toml() {
        let text = r#"
id = "roundtrip"
pattern = "fanout"

[defaults]
gates = ["contract", "fitness", { delta_guard = { protect = ["model/**"] } },
         { command = { cmd = "pytest -q", timeout_secs = 600 } }]

[[nodes]]
id = "a"
spec = "s"
"#;
        let plan = parse(text);
        let compiled = compile(plan.clone()).expect("compile");
        let dir = tempfile::tempdir().expect("tmp");
        let path = write_plan(&compiled.plan, dir.path()).expect("write");
        let back = parse_plan(&path).expect("read back");
        assert_eq!(back.defaults.gates.len(), 4);
        assert_eq!(back.defaults.gates[2].name(), "delta_guard");
        assert_eq!(back.defaults.gates[3].name(), "command");
        assert_eq!(back.pattern, PatternKind::Fanout);
    }

    #[test]
    fn duplicate_ids_rejected() {
        let text = "id = \"x\"\npattern = \"fanout\"\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\n\n[[nodes]]\nid = \"a\"\nspec = \"s2\"\n";
        let err = compile(parse(text)).expect_err("dup");
        assert!(err.to_string().contains("дубль"), "{err}");
    }

    #[test]
    fn cycle_is_detected_with_path() {
        let text = r#"
id = "x"
pattern = "dag"

[[nodes]]
id = "a"
spec = "s"
depends_on = ["b"]

[[nodes]]
id = "b"
spec = "s"
depends_on = ["a"]
"#;
        let err = compile(parse(text)).expect_err("cycle");
        let msg = err.to_string();
        assert!(msg.contains("цикл"), "{msg}");
        assert!(msg.contains('→'), "{msg}");
    }

    #[test]
    fn dangling_edge_rejected() {
        let text = "id = \"x\"\npattern = \"dag\"\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\n\n[[edges]]\nfrom = \"a\"\nto = \"ghost\"\n";
        let err = compile(parse(text)).expect_err("dangling");
        assert!(err.to_string().contains("ghost"), "{err}");
    }

    #[test]
    fn levels_are_topological_and_deterministic() {
        let text = r#"
id = "x"
pattern = "dag"

[[nodes]]
id = "c"
spec = "s"
depends_on = ["a", "b"]

[[nodes]]
id = "b"
spec = "s"
depends_on = ["a"]

[[nodes]]
id = "a"
spec = "s"
"#;
        let compiled = compile(parse(text)).expect("compile");
        let ids: Vec<Vec<&str>> = compiled
            .levels
            .iter()
            .map(|l| l.iter().map(|i| compiled.nodes()[*i].id.as_str()).collect())
            .collect();
        assert_eq!(ids, vec![vec!["a"], vec!["b"], vec!["c"]]);
    }

    #[test]
    fn map_reduce_adds_integrator_depending_on_all_workers() {
        let text = r#"
id = "x"
pattern = "map_reduce"

[[nodes]]
id = "w1"
spec = "s1"

[[nodes]]
id = "w2"
spec = "s2"
"#;
        let compiled = compile(parse(text)).expect("compile");
        let integrator = compiled
            .nodes()
            .iter()
            .find(|n| n.role == NodeRole::Integrator)
            .expect("integrator");
        assert_eq!(integrator.depends_on, vec!["w1", "w2"]);
        assert_eq!(compiled.levels.len(), 2);
    }

    #[test]
    fn tournament_adds_judge_per_group() {
        let text = r#"
id = "x"
pattern = "tournament"

[[nodes]]
id = "t1"
spec = "s"
group = "solve"

[[nodes]]
id = "t2"
spec = "s"
group = "solve"
"#;
        let compiled = compile(parse(text)).expect("compile");
        let judge = compiled
            .nodes()
            .iter()
            .find(|n| n.role == NodeRole::Judge)
            .expect("judge");
        assert_eq!(judge.depends_on, vec!["t1", "t2"]);
        assert_eq!(judge.id, "judge-solve");
    }

    #[test]
    fn review_pair_adds_reviewer_with_review_gate() {
        let text = r#"
id = "x"
pattern = "review_pair"

[[nodes]]
id = "impl"
spec = "s"
"#;
        let compiled = compile(parse(text)).expect("compile");
        let reviewer = compiled
            .nodes()
            .iter()
            .find(|n| n.role == NodeRole::Reviewer)
            .expect("reviewer");
        assert_eq!(reviewer.id, "review-impl");
        assert_eq!(reviewer.depends_on, vec!["impl"]);
        assert_eq!(
            compiled.gates_of("review-impl"),
            &[GateSpec::Review {
                min: ReviewVerdict::Ready
            }]
        );
    }

    #[test]
    fn walking_skeleton_orders_everything_after_skeleton() {
        let text = r#"
id = "x"
pattern = "walking_skeleton"

[policy]
stop_on_gate_fail = true

[skeleton]
id = "canary"
spec = "сквозной срез"

[[nodes]]
id = "w1"
spec = "s"
"#;
        let compiled = compile(parse(text)).expect("compile");
        let worker = compiled.node("w1").expect("w1");
        assert_eq!(worker.depends_on, vec!["canary"]);
        assert_eq!(
            compiled.node("canary").expect("canary").role,
            NodeRole::Skeleton
        );
    }

    #[test]
    fn pipeline_chain_from_order() {
        let text = r#"
id = "x"
pattern = "pipeline"
order = ["design", "impl"]

[[nodes]]
id = "design"
spec = "s"

[[nodes]]
id = "impl"
spec = "s"
"#;
        let compiled = compile(parse(text)).expect("compile");
        assert_eq!(
            compiled.node("impl").expect("impl").depends_on,
            vec!["design"]
        );
    }

    #[test]
    fn pipeline_without_order_or_deps_fails() {
        let text = "id = \"x\"\npattern = \"pipeline\"\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\n";
        let err = compile(parse(text)).expect_err("pipeline");
        assert!(err.to_string().contains("order"), "{err}");
    }

    #[test]
    fn ralph_requires_single_node() {
        let ok = "id = \"x\"\npattern = \"ralph\"\n\n[[nodes]]\nid = \"goal\"\nspec = \"s\"\n";
        let compiled = compile(parse(ok)).expect("compile");
        assert_eq!(compiled.nodes()[0].role, NodeRole::Ralph);
        let bad = "id = \"x\"\npattern = \"ralph\"\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\n\n[[nodes]]\nid = \"b\"\nspec = \"s\"\n";
        assert!(compile(parse(bad)).is_err());
    }

    #[test]
    fn fail_policy_forms_parse() {
        let text = r#"
id = "x"
pattern = "fanout"

[[nodes]]
id = "a"
spec = "s"
on_fail = { retry = 3 }
"#;
        let plan = parse(text);
        assert_eq!(plan.nodes[0].on_fail, FailPolicy::Retry { attempts: 3 });
        assert_eq!(plan.nodes[0].on_fail.max_attempts(), Some(3));
    }

    #[test]
    fn validate_reports_cycle_as_error() {
        let text = r#"
id = "x"
pattern = "dag"

[[nodes]]
id = "a"
spec = "s"
depends_on = ["b"]

[[nodes]]
id = "b"
spec = "s"
depends_on = ["a"]
"#;
        let issues = validate_plan(&parse(text));
        assert!(
            issues.iter().any(|i| i.severity == PlanSeverity::Error),
            "{issues:?}"
        );
    }

    #[test]
    fn validate_warns_about_gateless_nodes() {
        let text = "id = \"x\"\npattern = \"fanout\"\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\n";
        let issues = validate_plan(&parse(text));
        assert!(
            issues
                .iter()
                .any(|i| i.severity == PlanSeverity::Warn && i.message.contains("гейтов нет")),
            "{issues:?}"
        );
    }

    #[test]
    fn render_outputs_waves_and_mermaid() {
        let text = "id = \"x\"\npattern = \"pipeline\"\norder = [\"a\", \"b\"]\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\n\n[[nodes]]\nid = \"b\"\nspec = \"s\"\n";
        let compiled = compile(parse(text)).expect("compile");
        let text_out = render_plan(&compiled);
        assert!(text_out.contains("Волна 1"), "{text_out}");
        assert!(text_out.contains("Волна 2"), "{text_out}");
        let mermaid = render_mermaid(&compiled);
        assert!(mermaid.starts_with("graph TD"), "{mermaid}");
        assert!(mermaid.contains("a --> b"), "{mermaid}");
    }
}
