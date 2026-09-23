//! Конфигурация харнесса (`config.toml`).
//!
//! Порядок поиска конфига: `--config <path>` → `./arch-ml.toml` →
//! `~/.config/arch-ml/config.toml` → встроенные дефолты.
//! Команда `arch-ml init` пишет дефолтный конфиг и ассеты в [`Config::home_dir`].

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{HarnessError, Result};

/// Корневая конфигурация харнесса.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    /// Имя модели по умолчанию (ключ в [`Config::models`]).
    pub default_model: String,
    /// Настроенные LLM-провайдеры (OpenAI-совместимые endpoint'ы).
    pub models: BTreeMap<String, ModelConfig>,
    /// Параметры агентного цикла.
    pub agent: AgentConfig,
    /// Локальная база знаний (каталоги доменных документов).
    pub knowledge: KnowledgeConfig,
    /// Веб-доступ (поиск, фетч, кураторский список архитектурных сайтов).
    pub web: WebConfig,
    /// Интеграция Archify (Node CLI диаграмм JSON IR → HTML/SVG).
    pub archify: ArchifyConfig,
    /// Управление компьютером и браузером (vision + CDP, ADR-041).
    pub computer: ComputerConfig,
    /// Доменная база концептов (Ариадна): инструмент `concept_search`.
    pub concept: ConceptConfig,
    /// Адаптеры кодовых харнессов (Claude Code, Qwen Code, `OpenClaw`, …).
    pub harnesses: BTreeMap<String, CodingHarnessConfig>,
    /// Локальные GPU харнесса (`[[gpus.local]]`): своё железо, $0 аренды.
    pub gpus: GpusConfig,
    /// Настройки MCP.
    pub mcp: McpSettings,
    /// Каталоги плагинов (скиллы + MCP в одном пакете).
    pub plugins: PluginsConfig,
    /// Политика автономии инструментов (R-уровни).
    pub policy: PolicyConfig,
    /// Хуки жизненного цикла (shell-команды на событиях агента).
    pub hooks: HooksConfig,
    /// Изоляция окружения bash-команд (scrub секретоподобных переменных).
    pub bash: BashConfig,
    /// Настройки планировщика.
    pub cron: CronSettings,
    /// Ограничители goal-режима (контроль-петля автопродолжения).
    pub goal: GoalConfig,
    /// Настройки LLM-судьи рубрик (калибровка, ADR-004).
    pub judge: JudgeConfig,
    /// Настройки флота прогонов кодовых харнессов (изоляция и гейт мерджа).
    pub fleet: FleetConfig,
    /// Пороги маршрутизации значимости (Architecture Significance Score).
    pub significance: SignificanceConfig,
    /// Пути к ассетам, отчётам и сессиям.
    pub paths: PathsConfig,
    /// Оформление TUI (тема, цвет, глифы, минимум окна).
    pub tui: TuiConfig,
    /// Откуда конфиг загружен (нужно `harness_run` для горячего
    /// перечитывания адаптеров — правки config.toml подхватываются без
    /// перезапуска сессии). В файл не сериализуется.
    #[serde(skip)]
    pub loaded_from: Option<PathBuf>,
}

/// Конфигурация одного LLM-провайдера (OpenAI-совместимый API).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ModelConfig {
    /// Базовый URL API, напр. `https://api.deepseek.com/v1`.
    pub base_url: String,
    /// Идентификатор модели, напр. `deepseek-flash`.
    pub model: String,
    /// Имя переменной окружения с API-ключом.
    pub api_key_env: String,
    /// Запасной путь к файлу с ключом (`~` раскрывается): читается, если
    /// переменная окружения не задана (кейс theseus: ключ Kimi лежит в
    /// `~/.kimi_api_key`, а процесс харнесса env не видит). Содержимое
    /// никуда не выводится — только путь в тексте ошибки.
    #[serde(default)]
    pub api_key_file: Option<String>,
    /// URL прокси для этого провайдера, напр. `http://127.0.0.1:12080`.
    /// Для loopback-прокси (локальный egress-шлюз) харнесс при выборе модели
    /// проверяет порт и автоматически поднимает шлюз
    /// (`systemctl --user start vpn-egress`, см. [`crate::net`]); внешний
    /// прокси используется как есть. None — прямое соединение (или прокси
    /// из env `HTTP(S)_PROXY`, как раньше).
    #[serde(default)]
    pub proxy: Option<String>,
    /// Максимум токенов ответа.
    pub max_tokens: Option<u32>,
    /// Температура сэмплирования.
    pub temperature: Option<f32>,
    /// Бюджет тишины (сек): ожидание заголовков и пауза между чанками стрима.
    pub timeout_secs: u64,
    /// Окно контекста модели в токенах (если задано): автоматическая
    /// компактификация работает от `min(agent.context_budget_tokens`, этого
    /// окна) — пороги `compact_l1_pct`/`compact_l3_pct` (70%/95%) привязаны
    /// к реальному пределу API, а не к статичному бюджету.
    pub context_limit: Option<usize>,
    /// JSON-объект, сливаемый в тело запроса при включённом ризонинге
    /// (`/think on`): напр. `{"thinking": {"type": "enabled"}}` (`DeepSeek` V4,
    /// GLM-4.x) или `{"reasoning_effort": "max"}` (Kimi K3).
    /// None — переключение ризонинга для модели не настроено.
    pub thinking_on: Option<serde_json::Map<String, serde_json::Value>>,
    /// То же при `/think off`: `{"thinking": {"type": "disabled"}}` или
    /// `{"reasoning_effort": "low"}`.
    pub thinking_off: Option<serde_json::Map<String, serde_json::Value>>,
    /// Значение заголовка `User-Agent` для запросов этого провайдера.
    /// None — дефолт вендора (тонкая фабрика `GigaChat` ставит
    /// `spine-arch/<версия>`; без него API `GigaChat` отвечает 403).
    #[serde(default)]
    pub user_agent: Option<String>,
    /// Путь к PEM-файлу с корневым CA (напр. НУЦ Минцифры для GigaChat):
    /// корень добавляется в root store rustls-клиента. None — системные корни.
    #[serde(default)]
    pub ca_pem_file: Option<String>,
    /// Путь к PEM-файлу с клиентским сертификатом (mTLS-профиль `B2Bank`).
    /// Задаётся парой с `client_key_file`.
    #[serde(default)]
    pub client_cert_file: Option<String>,
    /// Путь к PEM-файлу с ключом клиентского сертификата (mTLS-профиль `B2Bank`).
    #[serde(default)]
    pub client_key_file: Option<String>,
    /// Параметры OAuth2-авторизации (вендоры с токеном вместо статического
    /// ключа — GigaChat): токен запрашивается с Basic-ключом из
    /// `api_key_env`/`api_key_file` и уходит как `Bearer` на вызовы API.
    /// None — OAuth не используется (mTLS-профиль `B2Bank` или статический ключ).
    #[serde(default)]
    pub oauth: Option<OAuthConfig>,
    /// Тариф: цена 1M входных (prompt) токенов в валюте пользователя
    /// (рубли, доллары — что удобно владельцу конфига; единица не
    /// фиксируется харнессом). None — тариф не задан: отчёты стоимости
    /// (`arch-ml metrics --cost-report`) показывают только токены.
    #[serde(default)]
    pub price_in_per_1m: Option<f64>,
    /// Тариф: цена 1M выходных (completion) токенов (см. `price_in_per_1m`).
    #[serde(default)]
    pub price_out_per_1m: Option<f64>,
}

impl Default for ModelConfig {
    fn default() -> Self {
        Self {
            base_url: String::new(),
            model: String::new(),
            api_key_env: String::new(),
            api_key_file: None,
            proxy: None,
            max_tokens: Some(8192),
            temperature: None,
            timeout_secs: 180,
            context_limit: None,
            thinking_on: None,
            thinking_off: None,
            user_agent: None,
            ca_pem_file: None,
            client_cert_file: None,
            client_key_file: None,
            oauth: None,
            price_in_per_1m: None,
            price_out_per_1m: None,
        }
    }
}

/// Параметры OAuth2-авторизации вендора (см. [`ModelConfig::oauth`]).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OAuthConfig {
    /// URL эндпоинта токена (`POST`, form-urlencoded `scope=…`).
    /// Пустая строка — дефолт профиля вендора (фабрика `GigaChat`
    /// подставляет `https://ngw.devices.sberbank.ru:9443/api/v2/oauth`),
    /// в конфиге дефолт не задаётся.
    #[serde(default)]
    pub token_url: String,
    /// Запрашиваемый scope: для `GigaChat` — `GIGACHAT_API_PERS` (физлица),
    /// `GIGACHAT_API_B2B`/`GIGACHAT_API_CORP` (юрлица) — выбор владельца
    /// инсталляции (от scope зависят лимиты одновременных потоков).
    pub scope: String,
}

impl ModelConfig {
    /// Читает API-ключ из переменной окружения (содержимое не логируется).
    ///
    /// # Errors
    /// Переменная окружения `api_key_env` не установлена.
    pub fn api_key(&self) -> Result<String> {
        std::env::var(&self.api_key_env).map_err(|_| {
            HarnessError::Config(format!(
                "переменная окружения {} не установлена (нужен API-ключ для {})",
                self.api_key_env, self.base_url
            ))
        })
    }
}

/// Параметры агентного цикла.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct AgentConfig {
    /// Максимум итераций «модель ↔ инструменты» за один ход.
    pub max_tool_turns: usize,
    /// Бюджет контекста в токенах (грубая оценка, 4 символа ≈ 1 токен).
    pub context_budget_tokens: usize,
    /// Стримить ответы модели (дельты в TUI/stdout).
    pub stream: bool,
    /// Порог L1-компактификации, % бюджета: маскирование старых
    /// tool-результатов (усечение с пометкой).
    pub compact_l1_pct: usize,
    /// Порог L3-компактификации, % бюджета: LLM-саммари истории в одно
    /// сообщение (последняя user-задача не трогается). >100 — отключено.
    pub compact_l3_pct: usize,
    /// Режим памяти сбоев инструментов (правило «ошибся дважды → урок»,
    /// см. модуль [`crate::failure_memory`]).
    pub failure_memory: FailureMemoryMode,
    /// Сколько последних сообщений с изображениями сохраняют картинки в
    /// запросе к модели. Изображения — самые тяжёлые элементы тела запроса
    /// (у `DeepSeek` лимит 48 MiB, потолок 600 изображений на запрос), а
    /// контекст растущей vision-сессии иначе переотправлял бы каждый старый
    /// скриншот каждый ход. 0 — не передавать ни одного, 1 — только текущий
    /// кадр (дефолт: для цикла «посмотрел → кликнул → посмотрел» хватает его).
    pub images_keep_last: usize,
}

/// Режим памяти сбоев инструментов (`[agent] failure_memory`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum FailureMemoryMode {
    /// Выключено: подписи сбоев не считаются.
    Off,
    /// Урок предлагается: заметка в транскрипт + предложение в
    /// `paths.state_dir/failure_lessons.md` (просмотр — `/lessons`).
    Propose,
    /// Как `propose`, плюс append урока в AGENTS.md рабочего каталога
    /// (вне зоны `ARCH:GENERATED`; только append, без перезаписи).
    Write,
}

impl FailureMemoryMode {
    /// Строковое имя режима (как в конфиге) — для показа в `/lessons`.
    #[must_use]
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Off => "off",
            Self::Propose => "propose",
            Self::Write => "write",
        }
    }
}

impl Default for AgentConfig {
    fn default() -> Self {
        Self {
            max_tool_turns: 4800,
            context_budget_tokens: 6_000_000,
            stream: true,
            compact_l1_pct: 70,
            compact_l3_pct: 95,
            failure_memory: FailureMemoryMode::Propose,
            images_keep_last: 1,
        }
    }
}

/// Локальная база знаний для `kb search`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct KnowledgeConfig {
    /// Каталоги с доменными документами.
    pub dirs: Vec<PathBuf>,
    /// Расширения файлов для индексации.
    pub extensions: Vec<String>,
}

impl Default for KnowledgeConfig {
    fn default() -> Self {
        let home = dirs::home_dir().unwrap_or_default();
        Self {
            // Нейтральные плейсхолдеры: свои каталоги задаются в config.toml.
            dirs: vec![
                home.join("knowledge/architecture"),
                home.join("knowledge/papers"),
                home.join("knowledge/skills"),
            ],
            extensions: vec!["md".into(), "txt".into(), "rst".into()],
        }
    }
}

/// Кураторский сайт архитектурных знаний.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArchSite {
    /// Короткое имя, напр. `aws-arch`.
    pub name: String,
    /// Базовый URL, напр. `https://docs.aws.amazon.com/architecture/`.
    pub base_url: String,
    /// Домен для site:-ограниченного поиска, напр. `docs.aws.amazon.com`.
    pub domain: String,
    /// Однострочное описание (чем сайт полезен архитектору).
    pub description: String,
}

/// Serde-дефолт для флагов, отсутствующих в конфиге: обратная совместимость —
/// новый ключ без явного значения не меняет поведение (включено).
fn default_true() -> bool {
    true
}

/// Serde-дефолт исполняемого файла Node.js для Archify-раннера.
pub(crate) fn default_node_bin() -> String {
    "node".to_owned()
}

/// Кандидаты исполняемого файла браузера для автодетекта (ADR-041):
/// первый найденный в `PATH` используется, если `browser_bin` пуст.
pub const DEFAULT_BROWSER_CANDIDATES: &[&str] = &[
    "google-chrome",
    "chromium",
    "chromium-browser",
    "firefox",
    "xdg-open",
];

/// Serde-дефолт таймаута вызова Archify CLI, сек.
fn default_archify_timeout() -> u64 {
    crate::archify::DEFAULT_TIMEOUT_SECS
}

/// Настройки интеграции Archify (Node CLI диаграмм JSON IR → HTML/SVG).
///
/// Личные пути машины (`cli_path`, `node_bin`) живут только здесь,
/// в пользовательском конфиге — никогда в коде репозитория (AGENTS).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ArchifyConfig {
    /// Выключатель: false — инструменты `archify_*` не регистрируются в
    /// инструментарии сессии; по умолчанию true.
    #[serde(default = "default_true")]
    pub enabled: bool,
    /// Путь к `bin/archify.mjs` установленного Archify (пусто — интеграция
    /// не настроена: инструменты отвечают инструкцией по установке).
    pub cli_path: PathBuf,
    /// Исполняемый файл Node.js (>=18).
    #[serde(default = "default_node_bin")]
    pub node_bin: String,
    /// Таймаут одного вызова CLI, секунды.
    #[serde(default = "default_archify_timeout")]
    pub timeout_secs: u64,
}

impl Default for ArchifyConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            cli_path: PathBuf::new(),
            node_bin: default_node_bin(),
            timeout_secs: default_archify_timeout(),
        }
    }
}

/// Управление компьютером и браузером через vision (ADR-041).
///
/// Контур `компьютер` — второй после `web` контур с настоящими внешними
/// последствиями: клик и ввод текста необратимы и видны за пределами
/// харнесса. Поэтому по умолчанию ВЫКЛЮЧЕН — в отличие от наблюдения
/// (`screenshot`/`read_image` в ядре, включены всегда).
///
/// Инструменты наблюдения (`screen_size`, `window_list`) живут в ядре и
/// этим гейтом не управляются: видеть экран безопасно, действовать — нет.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ComputerConfig {
    /// Выключатель: false — инструменты `computer_*` не регистрируются в
    /// инструментарии сессии; по умолчанию false (воздействие — опт-ин).
    pub enabled: bool,
    /// Бэкенд ввода: `xdotool` | `ydotool` | `wtype` (пусто — автодетект по
    /// `WAYLAND_DISPLAY`/`DISPLAY`). Личная настройка машины — только здесь.
    pub input_backend: String,
    /// Задержка между символами при наборе текста, мс (`xdotool type --delay`).
    pub key_delay_ms: u64,
    /// Потолок повторов в одном клике/скролле — тормоз против runaway-цикла.
    pub max_clicks: u32,
    /// Таймаут одного вызова инструмента ввода, секунды.
    pub timeout_secs: u64,
    /// Браузерный контур (запуск Chrome + CDP).
    pub browser: BrowserConfig,
}

impl Default for ComputerConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            input_backend: String::new(),
            key_delay_ms: 12,
            max_clicks: 3,
            timeout_secs: 10,
            browser: BrowserConfig::default(),
        }
    }
}

/// Браузерный контур: запуск Chrome/Chromium и управление через CDP.
///
/// Харнесс ВСЕГДА поднимает браузер с выделенным `--user-data-dir` внутри
/// `paths.state_dir` — профиль пользователя с его логинами не читается и не
/// модифицируется (ADR-041).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct BrowserConfig {
    /// Выключатель CDP-инструментов `browser_*`; по умолчанию false.
    pub enabled: bool,
    /// Исполняемый файл браузера (пусто — автодетект: google-chrome →
    /// chromium → chromium-browser → firefox → xdg-open).
    pub browser_bin: String,
    /// Исполняемый файл Node.js (>=18, нужен для CDP-хелпера).
    #[serde(default = "default_node_bin")]
    pub node_bin: String,
    /// Запускать браузер без окна (`--headless=new`).
    pub headless: bool,
    /// Порт отладочного протокола CDP (только loopback).
    pub debug_port: u16,
    /// Профиль браузера (пусто — `paths.state_dir/computer/chrome-profile`).
    pub user_data_dir: PathBuf,
    /// Таймаут одного вызова CDP-хелпера, секунды.
    pub timeout_secs: u64,
}

impl Default for BrowserConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            browser_bin: String::new(),
            node_bin: default_node_bin(),
            headless: false,
            debug_port: 9222,
            user_data_dir: PathBuf::new(),
            timeout_secs: 30,
        }
    }
}

/// Доменная база концептов (Ариадна) — инструмент `concept_search`.
/// Внешняя машинно-специфичная база: путь к индексу живёт только в
/// пользовательском конфиге/пресете (AGENTS), по умолчанию выключена.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ConceptConfig {
    /// Выключатель: false — `concept_search` не регистрируется
    /// (включается явно в пресете: `[concept].enabled = true`).
    pub enabled: bool,
    /// Путь к `concept_index.json` (пусто — инструмент отвечает инструкцией).
    pub index: PathBuf,
}

impl Default for ConceptConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            index: PathBuf::new(),
        }
    }
}

/// Настройки веб-доступа.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct WebConfig {
    /// Выключатель веб-канала: false — веб-инструменты не регистрируются в
    /// инструментарии сессии (egress-дисциплина контура домена); по умолчанию true.
    #[serde(default = "default_true")]
    pub enabled: bool,
    /// Endpoint поиска (`DuckDuckGo` HTML).
    pub search_base: String,
    /// User-Agent для запросов.
    pub user_agent: String,
    /// Таймаут запроса, секунды.
    pub timeout_secs: u64,
    /// Максимум символов текста после html→text.
    pub max_fetch_chars: usize,
    /// Кураторский список сайтов для архитектора.
    pub arch_sites: Vec<ArchSite>,
}

impl Default for WebConfig {
    fn default() -> Self {
        let site = |name: &str, base: &str, domain: &str, desc: &str| ArchSite {
            name: name.into(),
            base_url: base.into(),
            domain: domain.into(),
            description: desc.into(),
        };
        Self {
            enabled: true,
            search_base: "https://html.duckduckgo.com/html/".into(),
            user_agent: "arch-ml/0.1 (+solution-architect harness)".into(),
            timeout_secs: 30,
            max_fetch_chars: 24_000,
            arch_sites: vec![
                site(
                    "aws-arch",
                    "https://docs.aws.amazon.com/architecture/",
                    "docs.aws.amazon.com",
                    "AWS Architecture Center: reference architectures, Well-Architected",
                ),
                site(
                    "azure-arch",
                    "https://learn.microsoft.com/azure/architecture/",
                    "learn.microsoft.com",
                    "Azure Architecture Center: паттерны, reference architectures",
                ),
                site(
                    "gcp-arch",
                    "https://cloud.google.com/architecture",
                    "cloud.google.com",
                    "Google Cloud Architecture Center",
                ),
                site(
                    "fowler",
                    "https://martinfowler.com/architecture/",
                    "martinfowler.com",
                    "Мартин Фаулер: эссе по архитектуре, микросервисам, эволюционному дизайну",
                ),
                site(
                    "infoq-arch",
                    "https://www.infoq.com/architecture-design/",
                    "infoq.com",
                    "InfoQ Architecture & Design: статьи и тренды",
                ),
                site(
                    "microservices-io",
                    "https://microservices.io/",
                    "microservices.io",
                    "Каталог паттернов микросервисов (Крис Ричардсон)",
                ),
                site(
                    "c4",
                    "https://c4model.com/",
                    "c4model.com",
                    "C4 model: нотация визуализации архитектуры",
                ),
                site(
                    "arc42",
                    "https://docs.arc42.org/",
                    "docs.arc42.org",
                    "arc42: шаблон документирования архитектуры",
                ),
                site(
                    "togaf",
                    "https://pubs.opengroup.org/togaf-standard/",
                    "pubs.opengroup.org",
                    "TOGAF Standard (Open Group)",
                ),
                site(
                    "sei",
                    "https://insights.sei.cmu.edu/library/",
                    "insights.sei.cmu.edu",
                    "SEI/CAD: ATAM, архитектурные тактики, quality attributes",
                ),
                site(
                    "awesome-arch",
                    "https://awesome-architecture.com/",
                    "awesome-architecture.com",
                    "Кураторский список ресурсов по software architecture",
                ),
            ],
        }
    }
}

/// Как адаптер передаёт промпт кодовому харнессу.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PromptMode {
    /// Промпт — позиционный аргумент (`claude "..."`).
    Positional,
    /// Промпт через флаг (`agent --prompt "..."`).
    Flag,
    /// Промпт пишется в stdin.
    Stdin,
}

/// Адаптер кодового харнесса (Claude Code, Qwen Code, `OpenClaw`, Hermes, Theseus, `CodeWhale`, Kimi Code).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct CodingHarnessConfig {
    /// Имя бинаря в PATH.
    pub binary: String,
    /// Дополнительные аргументы (плейсхолдер `{prompt}` подставляется при `PromptMode::Flag`).
    pub args: Vec<String>,
    /// Режим передачи промпта.
    pub prompt_mode: PromptMode,
    /// Дополнительные переменные окружения.
    pub env: BTreeMap<String, String>,
    /// Whitelist наследуемых переменных окружения: при непустом списке процесс
    /// харнесса стартует с ЧИСТЫМ окружением (`env_clear`) и получает только
    /// перечисленные переменные + `env` адаптера. Пустой список — наследовать
    /// всё окружение процесса (поведение по умолчанию). Закрывает разрыв
    /// «окружение протекает между харнессами» (P1): чужие `*_MODEL`,
    /// прокси-переменные и ключи хоста не утекают в дочерний процесс.
    #[serde(default)]
    pub env_allow: Vec<String>,
    /// Таймаут прогона, секунды (абсолютный потолок).
    pub timeout_secs: u64,
    /// Таймаут тишины, секунды: прогон прерывается, если харнесс не пишет
    /// в stdout/stderr И не меняет файлы в репозитории дольше этого срока.
    /// 0 — отключить (только абсолютный потолок). Работающий молча харнесс
    /// (длинный tool-вызов внутри Claude Code и т.п.) при наличии активности
    /// файловой системы НЕ считается зависшим.
    pub idle_timeout_secs: u64,
    /// Авто-коммит незакоммиченных правок после успешного прогона
    /// (`Termination::Completed`): контракт TASK.md требует от исполнителя
    /// финального коммита, но исполнитель может его не сделать — тогда
    /// харнесс сам фиксирует оставшиеся изменения (кроме `.arch-handoff/`
    /// и мусора вида `__pycache__/`). Работа исполнителя всегда оказывается
    /// в git — это точка интеграции параллельных прогонов.
    pub auto_commit: bool,
    /// Транспорт прогона: отсутствует — процессный (задача через argv/stdin,
    /// ответ — stdout по итогу); `"acp"` — Agent Client Protocol v1 (ndjson
    /// JSON-RPC поверх stdio, ADR-049): живые `session/update` (видимость
    /// хода в логе), `session/resume` (контекст между прогонами), структурные
    /// разрешения и `usage_update`. В ACP-режиме `prompt_mode` не применяется:
    /// задача уходит через `session/prompt`, аргументы запуска — `args` как
    /// есть (например `binary="kimi"`, `args=["acp"]`).
    #[serde(default)]
    pub transport: Option<String>,
    /// ACP: алиас закреплённой сессии — политика «возобновить-или-создать»
    /// (ADR-049). None (дефолт) — каждый прогон открывает СВЕЖУЮ сессию
    /// (`session/new`): продолжение контекста — всегда осознанный выбор по
    /// имени. С алиасом: первый прогон открывает новую сессию и запоминает
    /// её за алиасом (карта `state/acp-sessions.json` в домашнем каталоге
    /// arch-ml), следующие продолжают контекст (`session/resume`);
    /// недоступная сессия или агент без `sessionCapabilities.resume` — не
    /// ошибка, а новая сессия с пометкой в логе прогона. Ключ карты —
    /// `харнесс/алиас`: одинаковые алиасы у разных харнессов не пересекаются.
    #[serde(default)]
    pub acp_session: Option<String>,
    /// ACP: политика ответов на `session/request_permission` (агент спрашивает
    /// разрешение на tool-вызов; без ответа он ждёт вечно):
    /// `"readonly_auto"` (дефолт) — `read`/`search`/`think`/`fetch`
    /// разрешаются автоматически, остальное отклоняется; `"deny"` — всё
    /// отклонять; `"allow_all"` — всё разрешать.
    #[serde(default)]
    pub acp_permission: Option<String>,
}

impl Default for CodingHarnessConfig {
    fn default() -> Self {
        Self {
            binary: String::new(),
            args: Vec::new(),
            prompt_mode: PromptMode::Positional,
            env: BTreeMap::new(),
            env_allow: Vec::new(),
            timeout_secs: 1800,
            idle_timeout_secs: 600,
            auto_commit: true,
            transport: None,
            acp_session: None,
            acp_permission: None,
        }
    }
}

/// Настройки MCP: путь к файлу серверов (формат как у Claude Code `mcp.json`).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct McpSettings {
    /// Путь к `mcp.json` (`{"mcpServers": {name: {command, args, env}}}`).
    pub servers_file: PathBuf,
    /// Подключаться к серверам при старте (иначе — лениво, по первому вызову).
    pub connect_on_start: bool,
    /// Таймаут MCP-вызова, секунды.
    pub timeout_secs: u64,
}

impl Default for McpSettings {
    fn default() -> Self {
        Self {
            servers_file: Config::home_dir().join("mcp.json"),
            connect_on_start: false,
            timeout_secs: 60,
        }
    }
}

/// Настройки плагинов: каталоги пакетов «скиллы + MCP»
/// (открытый стандарт agent-plugins.org: `plugin.json` + `skills/*/SKILL.md`).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct PluginsConfig {
    /// Каталоги с плагинами (каждый прямой потомок — плагин).
    pub dirs: Vec<PathBuf>,
    /// Подхватывать MCP-серверы из плагинов (`mcpServers` в plugin.json
    /// или `.mcp.json` в корне плагина) в общий MCP-пул.
    pub include_mcp: bool,
    /// Исполнять хуки плагинов (`hooks/hooks.json`): shell-команды из
    /// установленных плагинов — включайте только для доверенных библиотек.
    /// Дефолт `true` (как у `include_mcp`).
    pub include_hooks: bool,
}

impl Default for PluginsConfig {
    fn default() -> Self {
        Self {
            // По умолчанию — только библиотека харнесса; свои каталоги
            // плагинов добавляются в config.toml.
            dirs: vec![Config::home_dir().join("plugins")],
            include_mcp: true,
            include_hooks: true,
        }
    }
}

/// Хуки жизненного цикла (см. [`crate::hooks`]). Пусто — без хуков.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct HooksConfig {
    /// Спецификации хуков: `[[hooks.specs]]` с `event/tool/command/timeout_secs`.
    pub specs: Vec<crate::hooks::HookSpec>,
}

/// Изоляция окружения bash-команд (defensive pattern `DeepSeek` Harness:
/// «никогда не давай недоверенному выводу ambient-окружение»).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct BashConfig {
    /// Выбрасывать из окружения дочернего процесса переменные с
    /// секретоподобными именами (токены имени: KEY, SECRET, TOKEN,
    /// PASSWORD, PASSWD, PASS, CREDENTIAL(S), AUTH). Дефолт `true`:
    /// команды, которые пишет модель, не видят ключи провайдеров и не
    /// могут отправить их наружу (curl, логи, spill-файлы).
    pub env_scrub: bool,
    /// Точные имена переменных, которые пропускаются НЕСМОТРЯ на scrub
    /// (например `GH_TOKEN` для `gh`). Пусто — исключений нет.
    pub env_allow: Vec<String>,
    /// Изоляция дочерних команд: `"none"` (дефолт — текущее поведение) или
    /// `"bwrap"` (bubblewrap: рабочее дерево rw, остальная ФС ro, сети нет —
    /// egress дочерних процессов закрыт; см. ADR-038). Неизвестное значение —
    /// ошибка конфига при загрузке ([`Config::load`]).
    pub sandbox: String,
}

impl Default for BashConfig {
    fn default() -> Self {
        Self {
            env_scrub: true,
            env_allow: Vec::new(),
            sandbox: "none".into(),
        }
    }
}

/// Режим изоляции дочерних команд bash (`[bash] sandbox`, ADR-038).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BashSandbox {
    /// Без изоляции (поведение по умолчанию).
    None,
    /// bubblewrap: рабочее дерево rw, остальная ФС ro, `--unshare-net`.
    Bwrap,
}

impl BashConfig {
    /// Разобранное значение `sandbox`.
    ///
    /// # Errors
    /// Неизвестное значение: допустимы только `"none"` и `"bwrap"`.
    pub fn sandbox_mode(&self) -> Result<BashSandbox> {
        match self.sandbox.as_str() {
            "none" => Ok(BashSandbox::None),
            "bwrap" => Ok(BashSandbox::Bwrap),
            other => Err(HarnessError::Config(format!(
                "[bash] sandbox: неизвестное значение \"{other}\" \
                 (допустимы: \"none\", \"bwrap\")"
            ))),
        }
    }
}

/// Политика автономии инструментов (R-уровни R0–R5, по AI-Disrupt PDLC).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct PolicyConfig {
    /// Уровень автономии: R0 (всё через человека) … R5 (полная).
    /// Дефолт R2: чтения и изменения в рабочем каталоге — авто, деструктив —
    /// отказ. R4 — деструктив с подтверждением. R5 не рекомендуется.
    pub autonomy: String,
}

impl Default for PolicyConfig {
    fn default() -> Self {
        Self {
            autonomy: "R2".into(),
        }
    }
}

/// Настройки планировщика md-задач.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct CronSettings {
    /// Файл расписания (`cron.toml`).
    pub file: PathBuf,
    /// Каталог отчётов крона (по умолчанию `paths.reports_dir/cron`).
    pub out_dir: Option<PathBuf>,
}

impl Default for CronSettings {
    fn default() -> Self {
        Self {
            file: Config::home_dir().join("cron.toml"),
            out_dir: None,
        }
    }
}

/// Настройки goal-режима: контроль-петля автопродолжения (спека
/// `aiml/notes/goal-mode.md`). Состояние самой цели живёт в сессии, а не
/// в расписании — здесь только ограничители петли.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct GoalConfig {
    /// Максимум continuation-тернов одной цели: страховка от бесконечной
    /// петли (каждый терн — полноценный вызов модели).
    pub max_continuations: usize,
    /// Бюджет токенов прогона; `None` — без лимита (только счётчик в stats).
    pub token_budget: Option<u64>,
    /// Таймаут механической проверки критерия (`check`), секунды.
    pub check_timeout_secs: u64,
    /// Сколько повторов одного блокера переводят цель в `blocked`.
    pub blocker_repeats: usize,
    /// Сколько тернов подряд без нового вывода ведут к авто-паузе.
    pub no_progress_turns: usize,
}

impl Default for GoalConfig {
    fn default() -> Self {
        Self {
            max_continuations: 50,
            token_budget: None,
            check_timeout_secs: 60,
            blocker_repeats: 3,
            no_progress_turns: 3,
        }
    }
}

/// Настройки LLM-судьи рубрик (калибровка и верификация, ADR-004).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct JudgeConfig {
    /// Сэмплов судьи на критерий: независимые прогоны, итоговый балл —
    /// медиана сэмплов (1 — одиночная оценка, поведение до ADR-004).
    pub samples: usize,
    /// Порог population-σ баллов сэмплов: выше — критерий помечается
    /// флагом `unstable` (разброс судьи, к итогу балл допускается).
    pub unstable_stdev: f64,
    /// Минимальное сходство цитаты-свидетельства с оцениваемым текстом
    /// (0..=1; точный substring-матч засчитывается всегда). Цитата слабее
    /// порога при балле ≥ 2 → флаг `evidence_not_found`, критерий
    /// исключается из взвешенного итога.
    pub evidence_min_similarity: f64,
    /// Максимально допустимый MAE golden-прогона (`arch-ml bench run --golden`):
    /// итог выше порога → exit code 1 (регрессионный гейт качества судьи).
    pub golden_max_mae: f64,
    /// Ризонинг для запросов судьи: `Some(true)` — слить карту `thinking_on`
    /// модели (вернуть старое поведение); `Some(false)`/`None` (дефолт) —
    /// судья работает БЕЗ ризонинга: судья — задача структурной экстракции
    /// (JSON с баллами и цитатами), а thinking-токены входят в бюджет
    /// `max_tokens` провайдера и обрывали JSON посреди ответа
    /// (кейс 2026-09-01, deepseek-v4-flash).
    pub thinking: Option<bool>,
}

impl Default for JudgeConfig {
    fn default() -> Self {
        Self {
            samples: 3,
            unstable_stdev: 1.0,
            evidence_min_similarity: 0.8,
            golden_max_mae: 1.0,
            thinking: None,
        }
    }
}

/// Настройки флота прогонов кодовых харнессов: enforced-изоляция работы
/// агента и гейт интеграции (мотив — AI-native SDLC playbook Anthropic:
/// «агент не аппрувит свой код и не имеет пути в main» — enforced платформой,
/// а не дисциплиной промптов).
/// Оформление TUI (`[tui]`). CLI-флаги важнее этих значений; сюда пишут то,
/// что пользователь хочет видеть всегда, а не разово.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct TuiConfig {
    /// Тема: `auto` (по фону терминала), `dark` (Tokyo Night),
    /// `light` (Tokyo Night Day).
    pub theme: String,
    /// Захватывать мышь (колесо, выделение драгом, кнопка «▼»).
    /// Выключено — выделение текста делает сам терминал.
    pub mouse: bool,
    /// Анимации индикаторов (спиннер, «дыхание» мыслей). Выключено —
    /// статичные кадры: для ssh и медленных соединений.
    pub animation: bool,
    /// Unicode-рамки и глифы. Выключено — ASCII-фолбэк (`+-|`, `|/-\`).
    pub unicode: bool,
    /// Минимальная ширина окна: меньше — заглушка «терминал мал».
    pub min_width: u16,
    /// Минимальная высота окна.
    pub min_height: u16,
}

impl Default for TuiConfig {
    fn default() -> Self {
        Self {
            theme: "auto".into(),
            mouse: true,
            animation: true,
            unicode: true,
            min_width: 60,
            min_height: 16,
        }
    }
}

impl TuiConfig {
    /// Разбор темы; неизвестное значение — `None` (берём дефолт, не падаем).
    #[must_use]
    pub fn theme_choice(&self) -> Option<ThemeChoice> {
        ThemeChoice::parse(&self.theme)
    }
}

/// Выбор палитры TUI: тёмная, светлая или автоматически по фону терминала.
///
/// Живёт в `config` (нижний слой): конфигурация темы — понятие конфига,
/// поэтому слой `ui` обращается к типу снизу вверх (`no_tui_below_ui`),
/// а не наоборот. Используется `tui::caps` через re-export.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ThemeChoice {
    /// Определить по `COLORFGBG`; по умолчанию — тёмная.
    #[default]
    Auto,
    /// Тёмный фон (палитра Tokyo Night).
    Dark,
    /// Светлый фон (палитра Tokyo Night Day).
    Light,
    /// Контрастная для слабовидящих: без цвета и dim, смысл — bold/reverse
    /// (рекомендация скринридер-режима `tui-accessibility-compat` §7).
    HighContrast,
}

impl ThemeChoice {
    /// Разбор значения из конфига/CLI (`auto`/`dark`/`light`/`high-contrast`).
    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "auto" => Some(Self::Auto),
            "dark" => Some(Self::Dark),
            "light" => Some(Self::Light),
            "high-contrast" | "highcontrast" | "hc" => Some(Self::HighContrast),
            _ => None,
        }
    }

    /// Имя для диагностики (`--theme`, `arch-ml doctor`).
    #[must_use]
    pub fn name(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Dark => "dark",
            Self::Light => "light",
            Self::HighContrast => "high-contrast",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct FleetConfig {
    /// true — прогон кодового харнесса (`harness_run`, `arch harness-run`)
    /// обязан идти в изолированном git worktree (ветка `arch/<харнесс>-<timestamp>`,
    /// фабрика [`crate::worktree`]), а не в основном рабочем дереве; каталог
    /// не git-репозиторий — понятная ошибка ДО запуска харнесса.
    /// Дефолт `false` — поведение прежних версий (прогон в дереве как есть).
    pub require_worktree: bool,
    /// Гейт мерджа результата прогона в основную ветку (`arch fleet merge`):
    /// `owner` (дефолт) — мерж только с явным подтверждением владельца
    /// (`--owner-approve`); без флага печатается сводка прогона (diff stat,
    /// коммиты, статус контракта из evidence) и мерж отклоняется с exit 1.
    /// `none` — без гейта. Неизвестные значения трактуются как `owner`
    /// (безопасная интерпретация).
    pub merge_gate: String,
    /// Ёмкость каждого харнесса флота: сколько work-items он может гнать
    /// параллельно в прогоне `fleet run`. Роутер уважает это как потолок
    /// нагрузки (`capacity` в `AgentProfile`). Дефолт 1 — консервативно.
    pub capacity_per_agent: usize,
    /// Каталог планов флота в репозитории (ADR-042): `fleet plan propose`
    /// пишет сюда `<plan-id>.plan.toml`. Дефолт `.arch-fleet`.
    #[serde(default = "default_fleet_plan_dir")]
    pub plan_dir: PathBuf,
    /// Дефолтный потолок одновременных процессов плана, если план молчит.
    /// Движок без потолка не спавнит: сотни узлов в плане — да, одновременных
    /// процессов — сколько разрешил владелец. Дефолт 8.
    #[serde(default = "default_fleet_max_parallel")]
    pub max_parallel: usize,
    /// Дефолт `policy.stop_on_gate_fail` для планов, где он не задан.
    #[serde(default)]
    pub stop_on_gate_fail: bool,
    /// Дефолтный потолок времени одного узла, секунд (`0` — без лимита).
    #[serde(default)]
    pub budget_max_node_secs: u64,
    /// Дефолтный потолок суммарных попыток прогона (`0` — без лимита).
    #[serde(default)]
    pub budget_max_attempts: usize,
    /// Baseline-судья гейтов узлов (ADR-046): путь к бинарю `arch-ml`,
    /// собранному ДО прогона, — команда гейта, зовущая судью внутри ворктри
    /// узла, исполняется им, а не бинарём из оцениваемого дерева.
    /// Приоритет: явный `--judge`, затем переменная окружения `ARCH_ML_JUDGE`,
    /// затем это поле; иначе — бинарь, запустивший флот (если он вне ворктри).
    /// `None` — судья внутри ворктри запрещён (гейт падает), внешний — как
    /// раньше; правка ядра через `evolve` без судьи отклоняется.
    #[serde(default)]
    pub judge_binary: Option<PathBuf>,
}

/// Каталог планов флота по умолчанию (ADR-042).
fn default_fleet_plan_dir() -> PathBuf {
    PathBuf::from(crate::fleet_plan::FLEET_DIR)
}

/// Потолок одновременных процессов плана по умолчанию.
fn default_fleet_max_parallel() -> usize {
    crate::fleet_plan::DEFAULT_MAX_PARALLEL
}

impl Default for FleetConfig {
    fn default() -> Self {
        Self {
            require_worktree: false,
            merge_gate: "owner".into(),
            capacity_per_agent: 1,
            plan_dir: default_fleet_plan_dir(),
            max_parallel: default_fleet_max_parallel(),
            stop_on_gate_fail: false,
            budget_max_node_secs: 0,
            budget_max_attempts: 0,
            judge_binary: None,
        }
    }
}

/// Локальные GPU харнесса (`[[gpus.local]]`): своё железо ($0 аренды),
/// альтернатива облачному vast.ai. Объявляются в конфиге, сверяются с реестром
/// [`crate::gpu`]; выбор «куда гнать» — [`crate::gpu::recommend`].
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct GpusConfig {
    /// Объявленные локальные GPU.
    pub local: Vec<crate::gpu::LocalGpu>,
}

/// Пороги маршрутизации Architecture Significance Score (ADR-034).
///
/// Дефолты 1/4 воспроизводят историческую эвристику «0–1 → Fast, 2–4 →
/// Standard, 5+ → Critical» из `SOURCE_BRIEF` (статус — рабочая гипотеза,
/// пересмотр по данным пилота). Форсирующие critical-триггеры
/// (`security_boundary_change`, `irreversible_migration`,
/// `criticality_or_exception`) НЕ конфигурируются — fail-safe.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct SignificanceConfig {
    /// Верхняя граница маршрута Fast: score ≤ `fast_max` → Fast.
    pub fast_max: usize,
    /// Верхняя граница маршрута Standard: `fast_max` < score ≤ `standard_max` →
    /// Standard; выше — Critical.
    pub standard_max: usize,
}

impl Default for SignificanceConfig {
    fn default() -> Self {
        Self {
            fast_max: crate::control::DEFAULT_FAST_MAX,
            standard_max: crate::control::DEFAULT_STANDARD_MAX,
        }
    }
}

impl SignificanceConfig {
    /// Валидированные пороги `(fast_max, standard_max)`.
    ///
    /// Валидация мягкая — не при загрузке конфига, а при чтении в
    /// `control score`/`significance_score`: `fast_max` обязан быть строго
    /// меньше `standard_max`, иначе граница Fast/Standard неопределённа.
    ///
    /// # Errors
    /// `fast_max >= standard_max` — понятный текст ошибки.
    pub fn limits(&self) -> Result<(usize, usize)> {
        if self.fast_max >= self.standard_max {
            return Err(HarnessError::Config(format!(
                "[significance]: fast_max ({}) обязан быть меньше standard_max ({}) — \
                 иначе граница маршрутов Fast/Standard неопределённа",
                self.fast_max, self.standard_max
            )));
        }
        Ok((self.fast_max, self.standard_max))
    }
}

/// Пути к ассетам и данным харнесса.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct PathsConfig {
    /// Корень ассетов (prompts/, rubrics/, benchmarks/, ascii/).
    pub assets_dir: PathBuf,
    /// Каталог отчётов (рубрики, бенчи, контроль).
    pub reports_dir: PathBuf,
    /// Каталог сессий (append-only журналы агента).
    pub sessions_dir: PathBuf,
    /// Каталог состояния харнесса (персистентные счётчики и артефакты
    /// вроде `failure_memory.json`/`failure_lessons.md` модуля
    /// [`crate::failure_memory`]).
    pub state_dir: PathBuf,
    /// Глобальная md-память харнесса: содержимое дописывается в системный
    /// промпт каждой сессии (см. модуль `memory`).
    pub memory_file: PathBuf,
}

impl Default for PathsConfig {
    fn default() -> Self {
        let home = Config::home_dir();
        Self {
            assets_dir: home.join("assets"),
            reports_dir: home.join("reports"),
            sessions_dir: home.join("sessions"),
            state_dir: home.join("state"),
            memory_file: home.join("MEMORY.md"),
        }
    }
}

impl PathsConfig {
    /// Каталог библиотеки промптов.
    #[must_use]
    pub fn prompts_dir(&self) -> PathBuf {
        self.assets_dir.join("prompts")
    }

    /// Каталог якорных рубрик.
    #[must_use]
    pub fn rubrics_dir(&self) -> PathBuf {
        self.assets_dir.join("rubrics")
    }

    /// Каталог бенчмарков.
    #[must_use]
    pub fn benchmarks_dir(&self) -> PathBuf {
        self.assets_dir.join("benchmarks")
    }

    /// Каталог JSON-отчётов eval-сьютов: `evals/` в корне данных харнесса
    /// (по умолчанию `~/.arch-ml/evals`; следует за переопределением
    /// `reports_dir` — корнем считается его родитель).
    #[must_use]
    pub fn evals_dir(&self) -> PathBuf {
        self.reports_dir.parent().map_or_else(
            || Config::home_dir().join("evals"),
            |root| root.join("evals"),
        )
    }
}

/// JSON-карта параметра ризонинга (`thinking: {type: ...}`) для тела запроса.
type ThinkingMap = Option<serde_json::Map<String, serde_json::Value>>;

/// Карты ризонинга в стиле `thinking: {type: enabled/disabled}`
/// (`DeepSeek` V4, GLM-4.x/5.x): возвращает (on, off).
fn thinking_type_maps() -> (ThinkingMap, ThinkingMap) {
    let make = |kind: &str| {
        let mut inner = serde_json::Map::new();
        inner.insert("type".into(), kind.into());
        let mut outer = serde_json::Map::new();
        outer.insert("thinking".into(), inner.into());
        Some(outer)
    };
    (make("enabled"), make("disabled"))
}

impl Default for Config {
    fn default() -> Self {
        // Модели сверены с официальной документацией (август 2026):
        // - DeepSeek: deepseek-chat/reasoner сняты 2026-07-24 → v4-flash/v4-pro;
        //   deepseek-v4-flash выведена → deepseek-flash (V4.1-Flash, тариф Flash);
        //   ризонинг — `thinking: {type: enabled/disabled}` (api-docs.deepseek.com/
        //   guides/thinking_mode); с tools требуется эхо reasoning_content (см. llm.rs);
        // - Kimi: kimi-k2 снят 2025-05-25 → kimi-k3 (1M ctx), ризонинг —
        //   `reasoning_effort: low/high/max` (platform.kimi.ai/docs/models);
        // - GLM: glm-4.6 → glm-4.7, ризонинг — `thinking: {type: ...}` (docs.z.ai);
        //   glm-5.3-flash — окно 1.3M, thinking не отключается (см. ниже);
        // - GigaChat (сентябрь 2026, ADR-021): GigaChat-2/3, контекст 128K,
        //   OAuth2 вместо статического ключа (developers.sber.ru/docs/ru/gigachat).
        let mut models = BTreeMap::new();
        let (think_on, think_off) = thinking_type_maps();
        models.insert(
            "deepseek".into(),
            ModelConfig {
                base_url: "https://api.deepseek.com/v1".into(),
                model: "deepseek-flash".into(),
                api_key_env: "DEEPSEEK_API_KEY".into(),
                context_limit: Some(1_000_000),
                thinking_on: think_on.clone(),
                thinking_off: think_off.clone(),
                ..ModelConfig::default()
            },
        );
        models.insert(
            "deepseek-pro".into(),
            ModelConfig {
                base_url: "https://api.deepseek.com/v1".into(),
                model: "deepseek-v4-pro".into(),
                api_key_env: "DEEPSEEK_API_KEY".into(),
                timeout_secs: 300,
                context_limit: Some(1_000_000),
                thinking_on: think_on.clone(),
                thinking_off: think_off,
                ..ModelConfig::default()
            },
        );
        models.insert(
            "kimi".into(),
            ModelConfig {
                // Официальная coding-поверхность Kimi Code (доки kimi.com/code):
                // модели k3 / k3-256k; старая /v1 → 404. thinking-параметры
                // поверхность НЕ принимает (400 без reasoning_content в истории
                // — его харнесс эхом возвращает); temperature — только 1,
                // поэтому не шлём вовсе. Ключ — env KIMI_API_KEY или файл.
                base_url: "https://api.kimi.com/coding/v1".into(),
                model: "k3".into(),
                api_key_env: "KIMI_API_KEY".into(),
                api_key_file: Some("~/.kimi_api_key".into()),
                context_limit: Some(1_000_000),
                ..ModelConfig::default()
            },
        );
        let (glm_on, glm_off) = thinking_type_maps();
        let glm_entry = |model: &str, timeout: u64, context_limit: usize| ModelConfig {
            // Международная площадка Z.AI (для Китая — open.bigmodel.cn).
            base_url: "https://api.z.ai/api/paas/v4".into(),
            model: model.into(),
            api_key_env: "ZHIPU_API_KEY".into(),
            timeout_secs: timeout,
            context_limit: Some(context_limit),
            thinking_on: glm_on.clone(),
            thinking_off: glm_off.clone(),
            ..ModelConfig::default()
        };
        models.insert("glm".into(), glm_entry("glm-5.2", 180, 1_000_000));
        models.insert("glm-4.7".into(), glm_entry("glm-4.7", 180, 204_800));
        models.insert("glm-air".into(), glm_entry("glm-4.5-air", 120, 131_072));
        models.insert("glm-flash".into(), glm_entry("glm-4.7-flash", 120, 204_800));
        // GLM-5.3-Flash: окно 1.3M; thinking НЕ отключается (HTTP 1210 на
        // thinking.type=disabled, проверено 28.08) — «off» эмулируем картой
        // enabled + reasoning_effort=low (допустимы low|high|max).
        let glm53f_off = {
            let mut m = glm_on.clone().unwrap_or_default();
            m.insert("reasoning_effort".into(), "low".into());
            Some(m)
        };
        models.insert(
            "glm-5.3-flash".into(),
            ModelConfig {
                thinking_off: glm53f_off,
                ..glm_entry("glm-5.3-flash", 180, 1_310_720)
            },
        );
        // GigaChat (ADR-021, developers.sber.ru/docs/ru/gigachat): OAuth2 вместо
        // статического ключа — Basic-ключ из GIGACHAT_API_KEY идёт на
        // oauth.token_url (дефолт фабрики ngw.devices.sberbank.ru), далее Bearer
        // (TTL 30 мин, упреждающий рефреш). base_url не задан → пресет фабрики
        // https://api.giga.chat/v1; User-Agent обязателен (без него 403) —
        // ставит фабрика. thinking в API нет — карты ризонинга не задаём.
        // ca_pem_file в дефолт не включаем: путь к PEM корня НУЦ Минцифры —
        // машинно-зависимый (живой профиль — в config.example.toml).
        // scope — выбор владельца инсталляции (PERS — 1 поток; B2B/CORP — 10).
        let gigachat_entry = |model: &str, timeout: u64| ModelConfig {
            model: model.into(),
            api_key_env: "GIGACHAT_API_KEY".into(),
            timeout_secs: timeout,
            context_limit: Some(128_000),
            oauth: Some(OAuthConfig {
                token_url: String::new(),
                scope: "GIGACHAT_API_PERS".into(),
            }),
            ..ModelConfig::default()
        };
        models.insert("gigachat".into(), gigachat_entry("GigaChat-2-Pro", 180));
        models.insert("gigachat-max".into(), gigachat_entry("GigaChat-2-Max", 300));
        // GigaChat-3-Ultra: только физлица (Freemium; на платных тарифах и для
        // юрлиц недоступна — developers.sber.ru/docs/ru/gigachat/models/
        // gigachat-3-ultra, обновлено 17.07.2026). Согласуется со scope PERS.
        models.insert(
            "gigachat-ultra".into(),
            gigachat_entry("GigaChat-3-Ultra", 300),
        );
        // Корпоративные ключи (юрлица, scope B2B/CORP — 10 потоков) дефолтными
        // строками пикера не дублируем: подключение — копией любой секции
        // gigachat* в config.toml с заменой api_key_env (напр. на
        // GIGACHAT_API_KEY_CORP) и oauth.scope (шаблон в config.example.toml).

        let harness = |binary: &str, args: &[&str], mode: PromptMode| CodingHarnessConfig {
            binary: binary.into(),
            args: args.iter().map(|s| (*s).into()).collect(),
            prompt_mode: mode,
            ..CodingHarnessConfig::default()
        };
        let mut harnesses = BTreeMap::new();
        // Claude Code в headless (`-p` без TTY) на файловых операциях встаёт на
        // permission-промпте и ждёт вечно → --dangerously-skip-permissions
        // обязателен для unattended-прогонов. Права процесса ограничены
        // каталогом репозитория; уберите флаг в config.toml для интерактива.
        harnesses.insert(
            "claude-code".into(),
            harness(
                "claude",
                &["-p", "--dangerously-skip-permissions"],
                PromptMode::Stdin,
            ),
        );
        // Qwen Code: без флагов запускается ИНТЕРАКТИВНЫЙ CLI — дефолтная
        // подкоманда `qwen` («Launch Qwen Code [default]»), а справка прямо
        // говорит «use -p/--prompt for non-interactive mode». Независимый
        // проход дал args = [] + stdin → интерактив, а не headless.
        harnesses.insert(
            "qwen-code".into(),
            harness("qwen", &["-p", "{prompt}"], PromptMode::Flag),
        );
        // Флаги валидированы живыми прогонами флота (бенч 04_payment-idempotency,
        // 2026-08): неверные режимы/флаги давали код 2 на argparse.
        harnesses.insert(
            "openclaw".into(),
            harness(
                "openclaw",
                &["agent", "--agent", "main", "--message", "{prompt}"],
                PromptMode::Flag,
            ),
        );
        harnesses.insert(
            "hermes".into(),
            harness("hermes", &["-z", "{prompt}"], PromptMode::Flag),
        );
        harnesses.insert(
            "theseus".into(),
            harness("theseus", &["-p", "{prompt}"], PromptMode::Flag),
        );
        // Codewhale: документированный неинтерактивный путь — подкоманда
        // `exec` («Run a non-interactive prompt») с `--auto` («tool-backed
        // agent mode with auto-approvals»); примеры в её же справке:
        // `codewhale exec --auto "list crates/ with ls"`. Верхнеуровневый
        // `-p` описания не имеет, а верхнеуровневый запуск — интерактивная
        // сессия; живые конфиги уже переведены на `exec --auto`.
        harnesses.insert(
            "codewhale".into(),
            harness(
                "codewhale",
                &["exec", "--auto", "{prompt}"],
                PromptMode::Flag,
            ),
        );
        // Kimi Code: headless — `kimi -p PROMPT`. Permission-флаг НЕ нужен и
        // недопустим: в `-p`-режиме regular-инструменты всегда исполняются под
        // auto-политикой (без интерактивных промптов), а `--yolo`/`--auto` с
        // `--prompt` несовместимы — запуск отклоняется (документация
        // kimi-command, 2026-08). Модель — `-m <alias>` (по умолчанию
        // `default_model` из конфига kimi).
        harnesses.insert(
            "kimi-code".into(),
            harness("kimi", &["-p", "{prompt}"], PromptMode::Flag),
        );

        Self {
            default_model: "deepseek".into(),
            models,
            tui: TuiConfig::default(),
            agent: AgentConfig::default(),
            knowledge: KnowledgeConfig::default(),
            web: WebConfig::default(),
            archify: ArchifyConfig::default(),
            computer: ComputerConfig::default(),
            concept: ConceptConfig::default(),
            harnesses,
            gpus: GpusConfig {
                local: vec![
                    crate::gpu::LocalGpu {
                        name: "gb10".into(),
                        device: "gb10".into(),
                        vram_gb: None,
                        ssh: Some("gb10-fast".into()),
                    },
                    crate::gpu::LocalGpu {
                        name: "rtx4080super".into(),
                        device: "rtx4080super".into(),
                        vram_gb: None,
                        ssh: None,
                    },
                ],
            },
            mcp: McpSettings::default(),
            plugins: PluginsConfig::default(),
            policy: PolicyConfig::default(),
            hooks: HooksConfig::default(),
            bash: BashConfig::default(),
            cron: CronSettings::default(),
            goal: GoalConfig::default(),
            judge: JudgeConfig::default(),
            fleet: FleetConfig::default(),
            significance: SignificanceConfig::default(),
            paths: PathsConfig::default(),
            loaded_from: None,
        }
    }
}

impl Config {
    /// Домашний каталог харнесса (`~/.arch-ml` или `$ARCH_ML_HOME`).
    pub fn home_dir() -> PathBuf {
        std::env::var_os("ARCH_ML_HOME")
            .map(PathBuf::from)
            .or_else(|| dirs::home_dir().map(|h| h.join(".arch-ml")))
            .unwrap_or_else(|| PathBuf::from(".arch-ml"))
    }

    /// Каталог конфигурации (`~/.config/arch-ml`).
    #[must_use]
    pub fn config_dir() -> PathBuf {
        dirs::config_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join("arch-ml")
    }

    /// Загружает конфиг: `--config` → `./arch-ml.toml` →
    /// `~/.config/arch-ml/config.toml` → дефолты.
    ///
    /// # Errors
    /// Ошибка чтения/разбора файла, если явный путь задан и недоступен.
    pub fn load(cli_path: Option<&Path>) -> Result<Self> {
        let candidates: Vec<PathBuf> = match cli_path {
            Some(p) => vec![p.to_path_buf()],
            None => vec![
                PathBuf::from("arch-ml.toml"),
                Self::config_dir().join("config.toml"),
            ],
        };
        for path in &candidates {
            if path.is_file() {
                let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
                let mut cfg: Config = toml::from_str(&text)?;
                // Fail-fast валидация значений, которые serde не проверяет
                // (поле-строка с ограниченным набором значений).
                cfg.bash.sandbox_mode()?;
                cfg.expand_tildes();
                cfg.loaded_from = Some(path.clone());
                return Ok(cfg);
            }
        }
        let mut cfg = Config::default();
        cfg.expand_tildes();
        Ok(cfg)
    }

    /// Сохраняет конфиг в `~/.config/arch-ml/config.toml`.
    ///
    /// # Errors
    /// Ошибка создания каталога или записи файла.
    pub fn save_default(&self) -> Result<PathBuf> {
        let path = Self::config_dir().join("config.toml");
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
        }
        let text = toml::to_string_pretty(self)
            .map_err(|e| HarnessError::Config(format!("сериализация конфига: {e}")))?;
        std::fs::write(&path, text).map_err(|e| HarnessError::io(&path, e))?;
        Ok(path)
    }

    /// Подставляет `~` в начале путей (toml не раскрывает тильду).
    fn expand_tildes(&mut self) {
        let expand = |p: &mut PathBuf| {
            if let Ok(s) = p.clone().into_os_string().into_string() {
                if let Some(rest) = s.strip_prefix("~/") {
                    if let Some(home) = dirs::home_dir() {
                        *p = home.join(rest);
                    }
                }
            }
        };
        for d in &mut self.knowledge.dirs {
            expand(d);
        }
        expand(&mut self.mcp.servers_file);
        for d in &mut self.plugins.dirs {
            expand(d);
        }
        expand(&mut self.cron.file);
        if let Some(d) = &mut self.cron.out_dir {
            expand(d);
        }
        expand(&mut self.paths.assets_dir);
        expand(&mut self.paths.reports_dir);
        expand(&mut self.paths.sessions_dir);
        expand(&mut self.paths.state_dir);
        expand(&mut self.paths.memory_file);
        expand(&mut self.concept.index);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_has_three_providers() {
        let cfg = Config::default();
        for name in ["deepseek", "kimi", "glm"] {
            assert!(cfg.models.contains_key(name), "нет модели {name}");
        }
        assert!(cfg.harnesses.contains_key("claude-code"));
        assert!(cfg.harnesses.contains_key("codewhale"));
        assert!(cfg.harnesses.contains_key("kimi-code"));
        assert!(cfg.web.arch_sites.len() >= 8);
    }

    #[test]
    fn default_adapters_carry_validated_flags() {
        // Дефолты валидированы живыми прогонами флота (2026-08): неверный
        // флаг/режим давал argparse-код 2 (hermes: «unrecognized arguments: -p»).
        // Уточнение 2026-09-13: argparse-код 2 ловит только грубую ошибку
        // флага, но НЕ ловит «запустился интерактив вместо headless» — поэтому
        // qwen-code и codewhale приведены к документированному
        // неинтерактивному вызову своих CLI (см. комментарии у их дефолтов).
        // Прогонное подтверждение — `scripts/adapters-headless-check.sh`.
        let cfg = Config::default();
        let h = &cfg.harnesses;
        let flags = |name: &str| (h[name].args.clone(), h[name].prompt_mode);
        assert_eq!(
            flags("hermes"),
            (
                vec!["-z".to_string(), "{prompt}".to_string()],
                PromptMode::Flag
            )
        );
        assert_eq!(
            flags("qwen-code"),
            (
                vec!["-p".to_string(), "{prompt}".to_string()],
                PromptMode::Flag
            )
        );
        assert_eq!(
            flags("theseus"),
            (
                vec!["-p".to_string(), "{prompt}".to_string()],
                PromptMode::Flag
            )
        );
        assert_eq!(
            flags("codewhale"),
            (
                vec![
                    "exec".to_string(),
                    "--auto".to_string(),
                    "{prompt}".to_string()
                ],
                PromptMode::Flag
            )
        );
        // kimi-code: flag-режим `-p {prompt}`; permission-флагов нет осознанно —
        // `--yolo`/`--auto` несовместимы с `-p` (headless сам под auto-политикой).
        assert_eq!(
            flags("kimi-code"),
            (
                vec!["-p".to_string(), "{prompt}".to_string()],
                PromptMode::Flag
            )
        );
        assert!(!h["kimi-code"].args.iter().any(|a| a.contains("yolo")));
        assert_eq!(
            flags("openclaw"),
            (
                vec![
                    "agent".to_string(),
                    "--agent".to_string(),
                    "main".to_string(),
                    "--message".to_string(),
                    "{prompt}".to_string()
                ],
                PromptMode::Flag
            )
        );
    }

    #[test]
    fn load_remembers_config_path_for_hot_reload() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let path = tmp.path().join("config.toml");
        std::fs::write(&path, "default_model = \"glm\"\n").expect("write");
        let cfg = Config::load(Some(&path)).expect("load");
        assert_eq!(cfg.loaded_from.as_deref(), Some(path.as_path()));
        assert_eq!(cfg.default_model, "glm");
        // Снапшот дефолтов без файла — loaded_from пуст (горячего источника нет).
        assert!(Config::default().loaded_from.is_none());
        // loaded_from не утекает в сериализацию конфига.
        let text = toml::to_string_pretty(&cfg).expect("serialize");
        assert!(!text.contains("loaded_from"), "{text}");
    }

    #[test]
    fn flagship_models_default_to_1m_context() {
        let cfg = Config::default();
        for name in ["deepseek", "deepseek-pro", "kimi", "glm"] {
            let limit = cfg.models[name].context_limit.expect("context_limit задан");
            assert_eq!(limit, 1_000_000, "окно {name}");
        }
        // Бюджетные GLM остаются на своих окнах.
        assert_eq!(cfg.models["glm-air"].context_limit, Some(131_072));
    }

    /// glm-5.3-flash в дефолтах: окно 1.3M; «off» ризонинга эмулируется
    /// enabled+low (провайдер не даёт отключить thinking — HTTP 1210).
    #[test]
    fn glm53_flash_defaults_with_emulated_thinking_off() {
        let cfg = Config::default();
        let m = &cfg.models["glm-5.3-flash"];
        assert_eq!(m.model, "glm-5.3-flash");
        assert_eq!(m.context_limit, Some(1_310_720));
        assert_eq!(m.api_key_env, "ZHIPU_API_KEY");
        let on = m.thinking_on.as_ref().expect("thinking_on задан");
        assert_eq!(on["thinking"]["type"], "enabled");
        let off = m.thinking_off.as_ref().expect("thinking_off задан");
        assert_eq!(off["thinking"]["type"], "enabled");
        assert_eq!(off["reasoning_effort"], "low");
    }

    #[test]
    fn config_roundtrips_through_toml() {
        let cfg = Config::default();
        let text = toml::to_string_pretty(&cfg).expect("serialize");
        let back: Config = toml::from_str(&text).expect("deserialize");
        assert_eq!(back.default_model, cfg.default_model);
        assert_eq!(back.models.len(), cfg.models.len());
    }

    #[test]
    fn model_config_parses_optional_proxy() {
        // Поле задано — URL прокси попадает в конфиг провайдера.
        let mc: ModelConfig = toml::from_str(
            "base_url = \"https://openrouter.ai/api/v1\"\n\
             model = \"stealth/ox-alpha\"\n\
             api_key_env = \"OPENROUTER_API_KEY\"\n\
             proxy = \"http://127.0.0.1:12080\"\n",
        )
        .expect("deserialize");
        assert_eq!(mc.proxy.as_deref(), Some("http://127.0.0.1:12080"));
        // Без поля — None (serde default): прямое соединение, как раньше.
        let plain: ModelConfig = toml::from_str(
            "base_url = \"https://api.deepseek.com/v1\"\n\
             model = \"deepseek-v4-flash\"\n\
             api_key_env = \"DEEPSEEK_API_KEY\"\n",
        )
        .expect("deserialize");
        assert!(plain.proxy.is_none());
        assert!(ModelConfig::default().proxy.is_none());
    }

    #[test]
    fn old_style_model_config_without_new_fields_still_parses() {
        // Обратная совместимость (ADR-021): старые конфиги без user_agent,
        // ca_pem_file, client_cert_file/client_key_file и oauth читаются
        // без изменений — новые поля optional, serde default.
        let text = "[models.legacy]\n\
            base_url = \"https://api.example.test/v1\"\n\
            model = \"legacy-model\"\n\
            api_key_env = \"LEGACY_KEY\"\n";
        let cfg: Config = toml::from_str(text).expect("parse");
        let mc = &cfg.models["legacy"];
        assert_eq!(mc.model, "legacy-model");
        assert_eq!(mc.timeout_secs, 180, "дефолты на месте");
        assert!(mc.user_agent.is_none());
        assert!(mc.ca_pem_file.is_none());
        assert!(mc.client_cert_file.is_none());
        assert!(mc.client_key_file.is_none());
        assert!(mc.oauth.is_none());
        assert!(mc.proxy.is_none());
    }

    #[test]
    fn oauth_config_parses_with_vendor_default_token_url() {
        // oauth-блок с пустым/отсутствующим token_url валиден: дефолт URL
        // профиля задаёт фабрика вендора (GigaChat), не конфиг.
        let text = "[models.gigachat]\n\
            model = \"GigaChat-2-Pro\"\n\
            api_key_env = \"GIGACHAT_KEY\"\n\
            [models.gigachat.oauth]\n\
            scope = \"GIGACHAT_API_PERS\"\n";
        let cfg: Config = toml::from_str(text).expect("parse");
        let oauth = cfg.models["gigachat"].oauth.as_ref().expect("oauth");
        assert_eq!(oauth.scope, "GIGACHAT_API_PERS");
        assert!(oauth.token_url.is_empty(), "дефолт задаст фабрика");
    }

    #[test]
    fn agent_failure_memory_mode_defaults_and_parses() {
        // Дефолт — propose: урок предлагается, AGENTS.md не трогается.
        let cfg = Config::default();
        assert_eq!(cfg.agent.failure_memory, FailureMemoryMode::Propose);
        // Разбор всех режимов из toml + круговой обход сериализации.
        for (text, want) in [
            ("off", FailureMemoryMode::Off),
            ("propose", FailureMemoryMode::Propose),
            ("write", FailureMemoryMode::Write),
        ] {
            let cfg: Config = toml::from_str(&format!("[agent]\nfailure_memory = \"{text}\"\n"))
                .expect("deserialize");
            assert_eq!(cfg.agent.failure_memory, want, "режим {text}");
            assert_eq!(cfg.agent.failure_memory.as_str(), text);
        }
        // Неизвестный режим — ошибка разбора, не молчаливый дефолт.
        assert!(toml::from_str::<Config>("[agent]\nfailure_memory = \"yolo\"\n").is_err());
        // state_dir — в домашнем каталоге харнесса.
        assert!(cfg.paths.state_dir.ends_with("state"));
    }

    #[test]
    fn goal_config_defaults_and_parse() {
        let cfg = Config::default();
        assert_eq!(cfg.goal.max_continuations, 50, "лимит тернов петли");
        assert_eq!(cfg.goal.check_timeout_secs, 60, "таймаут проверки критерия");
        assert_eq!(cfg.goal.blocker_repeats, 3, "порог блокера");
        assert_eq!(cfg.goal.no_progress_turns, 3, "порог пустых тернов");
        assert_eq!(cfg.goal.token_budget, None, "бюджет по умолчанию не задан");
        // Пустая секция [goal] в toml даёт те же дефолты (serde default):
        // старые конфиги без секции продолжают работать.
        let back: Config = toml::from_str("[goal]\n").expect("deserialize");
        assert_eq!(back.goal.max_continuations, 50);
        assert_eq!(back.goal.check_timeout_secs, 60);
    }

    #[test]
    fn judge_config_defaults_match_adr004() {
        let cfg = Config::default();
        assert_eq!(cfg.judge.samples, 3, "k сэмплов судьи по умолчанию");
        assert!((cfg.judge.unstable_stdev - 1.0).abs() < 1e-9);
        assert!((cfg.judge.evidence_min_similarity - 0.8).abs() < 1e-9);
        assert!((cfg.judge.golden_max_mae - 1.0).abs() < 1e-9);
        // Пустая секция [judge] в toml даёт те же дефолты (serde default).
        let back: Config = toml::from_str("[judge]\n").expect("deserialize");
        assert_eq!(back.judge.samples, 3);
    }

    #[test]
    fn fleet_config_defaults_and_parse() {
        // Дефолты: enforcement выключен (поведение прежних версий), гейт — owner.
        let cfg = Config::default();
        assert!(!cfg.fleet.require_worktree);
        assert_eq!(cfg.fleet.merge_gate, "owner");
        // Секция отсутствует в toml — serde default даёт те же значения.
        let bare: Config = toml::from_str("").expect("deserialize empty");
        assert!(!bare.fleet.require_worktree);
        assert_eq!(bare.fleet.merge_gate, "owner");
        // Явная секция парсится.
        let on: Config =
            toml::from_str("[fleet]\nrequire_worktree = true\nmerge_gate = \"none\"\n")
                .expect("deserialize");
        assert!(on.fleet.require_worktree);
        assert_eq!(on.fleet.merge_gate, "none");
        // Baseline-судья (ADR-046): по умолчанию не задан; явный путь парсится.
        assert!(cfg.fleet.judge_binary.is_none());
        assert!(bare.fleet.judge_binary.is_none());
        let with_judge: Config =
            toml::from_str("[fleet]\njudge_binary = \"/opt/arch-ml\"\n").expect("deserialize");
        assert_eq!(
            with_judge.fleet.judge_binary.as_deref(),
            Some(Path::new("/opt/arch-ml"))
        );
        // Round-trip через сериализацию.
        let text = toml::to_string_pretty(&on).expect("serialize");
        let back: Config = toml::from_str(&text).expect("deserialize");
        assert!(back.fleet.require_worktree);
        assert_eq!(back.fleet.merge_gate, "none");
    }

    #[test]
    fn web_defaults_to_enabled_when_key_missing() {
        // Обратная совместимость: без ключа enabled веб-канал включён, как раньше.
        let back: Config =
            toml::from_str("[web]\nsearch_base = \"http://127.0.0.1:9/\"\n").expect("deserialize");
        assert!(back.web.enabled);
        assert_eq!(back.web.search_base, "http://127.0.0.1:9/");
        // Полностью пустая секция [web] — тоже дефолт true (serde default).
        let empty: Config = toml::from_str("[web]\n").expect("deserialize");
        assert!(empty.web.enabled);
    }

    #[test]
    fn web_enabled_false_parses() {
        let back: Config = toml::from_str("[web]\nenabled = false\n").expect("deserialize");
        assert!(
            !back.web.enabled,
            "флаг [web].enabled=false должен читаться"
        );
    }

    #[test]
    fn significance_config_defaults_parse_and_validate() {
        // Дефолты 1/4 — историческая эвристика SOURCE_BRIEF (ADR-034):
        // обратная совместимость с поведением до введения секции.
        let cfg = Config::default();
        assert_eq!(cfg.significance.limits().expect("дефолты валидны"), (1, 4));
        // Секция отсутствует в toml — serde default даёт те же значения.
        let bare: Config = toml::from_str("").expect("deserialize empty");
        assert_eq!(bare.significance.limits().expect("валидны"), (1, 4));
        // Кастомные пороги парсятся и валидируются.
        let custom: Config =
            toml::from_str("[significance]\nfast_max = 2\nstandard_max = 6\n").expect("parse");
        assert_eq!(custom.significance.limits().expect("валидны"), (2, 6));
        // fast_max >= standard_max — мягкая ошибка при чтении (не при загрузке).
        let bad: Config =
            toml::from_str("[significance]\nfast_max = 4\nstandard_max = 4\n").expect("parse");
        let err = bad.significance.limits().expect_err("границы совпали");
        assert!(err.to_string().contains("fast_max"), "{err}");
    }

    #[test]
    fn bash_sandbox_defaults_parse_and_validate() {
        // Дефолт — "none": поведение прежних версий (без изоляции).
        let cfg = Config::default();
        assert_eq!(
            cfg.bash.sandbox_mode().expect("дефолт валиден"),
            BashSandbox::None
        );
        // Секция отсутствует в toml — serde default даёт то же значение.
        let bare: Config = toml::from_str("").expect("deserialize empty");
        assert_eq!(bare.bash.sandbox, "none");
        // bwrap парсится.
        let bwrap: Config = toml::from_str("[bash]\nsandbox = \"bwrap\"\n").expect("deserialize");
        assert_eq!(
            bwrap.bash.sandbox_mode().expect("валиден"),
            BashSandbox::Bwrap
        );
        // Неизвестное значение — понятная ошибка конфига, не молчаливый дефолт.
        let bad: Config = toml::from_str("[bash]\nsandbox = \"firejail\"\n").expect("deserialize");
        let err = bad.bash.sandbox_mode().expect_err("неизвестное значение");
        let text = err.to_string();
        assert!(text.contains("firejail"), "{text}");
        assert!(text.contains("none") && text.contains("bwrap"), "{text}");
    }

    #[test]
    fn load_rejects_unknown_bash_sandbox() {
        // Ошибка всплывает при загрузке файла конфига (fail-fast), а не при
        // первом запуске команды.
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("config.toml");
        std::fs::write(&path, "[bash]\nsandbox = \"nsjail\"\n").expect("write");
        let err = Config::load(Some(&path)).expect_err("загрузка должна упасть");
        assert!(err.to_string().contains("nsjail"), "{err}");
    }

    #[test]
    fn evals_dir_follows_reports_dir_root() {
        // Дефолт: ~/.arch-ml/evals (корень — родитель reports_dir).
        let cfg = Config::default();
        assert_eq!(
            cfg.paths.evals_dir(),
            Config::home_dir().join("evals"),
            "evals рядом с корнем данных"
        );
        // Переопределение reports_dir тащит evals в тот же корень
        // (детерминизм тестов и пользовательских макетов).
        let mut cfg = Config::default();
        cfg.paths.reports_dir = PathBuf::from("/tmp/x/reports");
        assert_eq!(cfg.paths.evals_dir(), PathBuf::from("/tmp/x/evals"));
    }
}
