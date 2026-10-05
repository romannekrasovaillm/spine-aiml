//! Командный слой бинаря `arch-ml`: clap-дерево [`Cli`]/[`Cmd`], диспетчер
//! [`dispatch`] и тела команд по тематическим модулям `cli/*` (DEF-3 —
//! декомпозиция `main.rs`, 2026-10-05; поведение команд неизменно — чистый
//! перенос без смены логики).

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};

use arch_harness::config::Config;

pub(crate) mod adapters;
pub(crate) mod archify;
pub(crate) mod case;
pub(crate) mod control;
pub(crate) mod eval;
pub(crate) mod fleet;
pub(crate) mod governance;
pub(crate) mod harness;
pub(crate) mod knowledge;
pub(crate) mod misc;
pub(crate) mod ml;
pub(crate) mod model;
pub(crate) mod run;

pub(crate) use adapters::{AgentsMdCmd, OpenspecCmd, PublishCmd};
pub(crate) use archify::ArchifyCmd;
pub(crate) use case::{DeltaCmd, EvidenceCmd};
pub(crate) use control::{ArchunitCmd, ControlCmd};
pub(crate) use eval::{BenchCmd, EvalCmd, RubricCmd};
pub(crate) use fleet::{FleetCmd, WorktreeCmd};
pub(crate) use governance::{AcceptCmd, EvolveCmd, GovernCmd};
pub(crate) use harness::McpCmd;
pub(crate) use knowledge::{PluginsCmd, SkillsCmd, WebCmd};
pub(crate) use misc::{CronCmd, MemoryCmd};
pub(crate) use ml::{DataCardCmd, ExperimentCmd, ResourcesCmd, TrajectoryCmd, WeightsCmd};
pub(crate) use model::{AdrCmd, ModelCmd, NfrCmd, TraceCmd};

/// Переопределения оформления TUI из глобальных флагов. Раньше конфига и
/// окружения: пользователь сказал явно — это важнее эвристики.
///
/// # Errors
/// Неизвестное значение `--theme` (ожидаются `auto`, `dark`, `light`).
fn tui_overrides(cli: &Cli) -> anyhow::Result<arch_harness::tui::caps::Overrides> {
    use arch_harness::config::ThemeChoice;
    use arch_harness::tui::caps::Overrides;
    let theme = cli
        .theme
        .as_deref()
        .map(|t| {
            ThemeChoice::parse(t).ok_or_else(|| {
                anyhow::anyhow!(
                    "--theme: ожидается auto, dark, light или high-contrast, получено «{t}»"
                )
            })
        })
        .transpose()?;
    Ok(Overrides {
        no_color: cli.no_color,
        ascii: cli.ascii,
        no_mouse: cli.no_mouse,
        no_animation: cli.no_animation,
        theme,
    })
}

/// Доменный харнесс solution-архитектора.
#[derive(Parser)]
#[command(name = "arch-ml", version, about, long_about = None)]
pub(crate) struct Cli {
    /// Путь к config.toml (иначе ./arch-ml.toml или ~/.config/arch-ml/config.toml).
    #[arg(long, global = true)]
    config: Option<PathBuf>,

    /// Тема TUI: auto (по фону терминала), dark, light.
    #[arg(long, global = true, value_name = "dark|light|auto")]
    theme: Option<String>,

    /// Без цвета: смысл несут символы и атрибуты (`NO_COLOR` тоже работает).
    #[arg(long, global = true)]
    no_color: bool,

    /// ASCII-рамки и глифы вместо Unicode (для LANG=C и старых консолей).
    #[arg(long, global = true)]
    ascii: bool,

    /// Не захватывать мышь: выделение текста делает терминал (Shift+drag).
    #[arg(long, global = true)]
    no_mouse: bool,

    /// Статичные индикаторы вместо анимации (ssh, медленные соединения).
    #[arg(long, global = true)]
    no_animation: bool,

    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand)]
pub(crate) enum Cmd {
    /// Интерактивный TUI (действие по умолчанию).
    Tui,
    /// Инициализация ~/.arch-ml: конфиг, ассеты, примеры.
    Init,
    /// Headless-прогон агента: `arch-ml run "задача"` или `cat spec.md | arch-ml run -`.
    Run {
        /// Промпт; `-` или отсутствие значения при пайпе — читать stdin.
        prompt: Option<String>,
        /// Модель (имя из [models]).
        #[arg(long)]
        model: Option<String>,
        /// Без стриминга (печатать только финальный ответ).
        #[arg(long)]
        no_stream: bool,
        /// Строгий headless-контракт (как `dsh --profile headless`):
        /// stdout — только финальный ответ ассистента, прогресс молчит;
        /// при успехе stderr пуст, при сбое — причина в stderr и exit 1.
        /// Для скриптов и пайпов: `arch-ml run -q "…" > answer.md`.
        #[arg(long, short = 'q')]
        quiet: bool,
        /// Общий таймаут прогона в секундах: по истечении — причина в stderr
        /// и exit 1 (страховка CI/cron от зависшего провайдера).
        #[arg(long, value_name = "SECS")]
        timeout: Option<u64>,
        /// Лимит итераций инструментов на этот прогон (перекрывает
        /// `agent.max_tool_turns` из конфига).
        #[arg(long, value_name = "N", value_parser = clap::value_parser!(u64).range(1..))]
        max_turns: Option<u64>,
        /// Ризонинг-режим: on|off (в запросы сливается карта `thinking_on/off`
        /// из конфига модели; без флага — дефолт провайдера).
        #[arg(long, value_name = "on|off")]
        think: Option<String>,
        /// Goal-режим: цель с механической проверкой критерия
        /// (`<что должно стать истиной> || <проверяемое состояние> ||
        /// <команда проверки>`, exit 0 — критерий выполнен). Промпт не нужен:
        /// первый терн запускает цель. Коды выхода: 0 complete, 3 blocked,
        /// 6 paused.
        #[arg(long, value_name = "OBJECTIVE || CRITERION || CHECK")]
        goal: Option<String>,
    },
    /// Список настроенных моделей.
    Models,
    /// Библиотека промптов: список или показ шаблона.
    Prompts {
        /// Имя шаблона (без — список).
        name: Option<String>,
    },
    /// Глобальная md-память (MEMORY.md): показать путь и содержимое.
    Memory {
        #[command(subcommand)]
        cmd: Option<MemoryCmd>,
    },
    /// Формальная подотчётность: именной аппрувер + журнал решений (H1.1).
    Govern {
        #[command(subcommand)]
        cmd: Option<GovernCmd>,
    },
    /// Независимый eval-бар: человеко-заданные приёмочные тесты (H1.2).
    Accept {
        #[command(subcommand)]
        cmd: Option<AcceptCmd>,
    },
    /// Guarded harness evolution (H2.2): propose/commit изменений доменного слоя.
    Evolve {
        #[command(subcommand)]
        cmd: Option<EvolveCmd>,
    },
    /// Рендер mermaid-файла в Unicode/ASCII-арт.
    Mermaid {
        /// Файл с диаграммой (`-` — stdin).
        file: String,
    },
    /// Archify: валидация, доставка и сравнение диаграмм (JSON IR → HTML/SVG).
    Archify {
        #[command(subcommand)]
        cmd: ArchifyCmd,
    },
    /// Рубрики архитектурного контроля.
    Rubric {
        #[command(subcommand)]
        cmd: RubricCmd,
    },
    /// Архитектурные бенчмарки.
    Bench {
        #[command(subcommand)]
        cmd: BenchCmd,
    },
    /// Поиск по локальной базе знаний.
    Kb {
        /// Запрос.
        query: String,
        /// Максимум результатов.
        #[arg(long, default_value_t = 8)]
        limit: usize,
    },
    /// Веб: поиск и фетч по архитектурным сайтам.
    Web {
        #[command(subcommand)]
        cmd: WebCmd,
    },
    /// MCP-серверы: список и вызовы.
    Mcp {
        #[command(subcommand)]
        cmd: McpCmd,
    },
    /// Сформировать handoff-пакет для кодового харнесса.
    Handoff {
        /// Имя харнесса (claude-code, qwen-code, openclaw, hermes, theseus, codewhale, kimi-code).
        harness: String,
        /// Путь к репозиторию.
        #[arg(long)]
        repo: PathBuf,
        /// Формулировка задачи.
        #[arg(long)]
        task: String,
        /// Файлы спек/спайна/ADR для включения.
        #[arg(long)]
        spec: Vec<PathBuf>,
        /// Явный план отката (иначе — откат на baseline-коммит).
        #[arg(long)]
        rollback: Option<String>,
        /// Маршрут значимости: fast|standard|critical (таймаут прогона: 1800/3600/7200 с).
        #[arg(long, default_value = "standard")]
        route: String,
    },
    /// Прогнать кодовый харнесс по handoff-пакету.
    HarnessRun {
        /// Имя харнесса.
        harness: String,
        /// Путь к репозиторию (с .arch-handoff/).
        #[arg(long)]
        repo: PathBuf,
        /// Задача (иначе — из .arch-handoff/TASK.md).
        #[arg(long)]
        task: Option<String>,
    },
    /// Список известных кодовых харнессов.
    Harnesses,
    /// Архитектурный контроль.
    Control {
        #[command(subcommand)]
        cmd: ControlCmd,
    },
    /// Реестр ADR: глобальная агрегация решений по набору проектов (ADR-036).
    Adr {
        #[command(subcommand)]
        cmd: AdrCmd,
    },
    /// Публикация артефактов в корпоративные системы (файловые адаптеры,
    /// ADR-033: git и файлы — транспорт, живых коннекторов нет).
    Publish {
        #[command(subcommand)]
        cmd: PublishCmd,
    },
    /// Типизированная модель архитектуры (каталог model/, ADR-003).
    Model {
        #[command(subcommand)]
        cmd: ModelCmd,
    },
    /// Трассируемость модели как fitness-функция (ADR-006).
    Trace {
        #[command(subcommand)]
        cmd: TraceCmd,
    },
    /// Количественные NFR поверх модели: latency-бюджет, доступность,
    /// ёмкость, стоимость (ADR-007).
    Nfr {
        #[command(subcommand)]
        cmd: NfrCmd,
    },
    /// Библиотека скиллов: список, поиск, показ.
    Skills {
        #[command(subcommand)]
        cmd: SkillsCmd,
    },
    /// Плагины (скиллы + MCP в одном пакете).
    Plugins {
        #[command(subcommand)]
        cmd: PluginsCmd,
    },
    /// Политика автономии (R-уровни): показать/проверить класс риска команды.
    Policy {
        /// Проверить команду: как её классифицирует политика.
        #[arg(long)]
        check: Option<String>,
    },
    /// Evidence Bundle — аудиторский след как условие выпуска.
    Evidence {
        #[command(subcommand)]
        cmd: EvidenceCmd,
    },
    /// Операционные метрики харнесса (из журналов сессий и отчётов).
    Metrics {
        /// Смета по реальному usage: таблица по моделям, топ-10 дорогих сессий.
        #[arg(long)]
        cost_report: bool,
    },
    /// Локальные GPU харнесса: инвентарь, живая детекция, выбор «куда гнать».
    Resources {
        #[command(subcommand)]
        cmd: ResourcesCmd,
    },
    /// Provenance эксперимента: записать «что именно гоняли» и воспроизвести.
    Experiment {
        #[command(subcommand)]
        cmd: ExperimentCmd,
    },
    /// Детерминированный роутер экспертных моделей Ariadna (ось 4).
    Ariadna {
        /// Доменный вопрос для маршрутизации (v10/v1/both/frontier).
        question: String,
    },
    /// Диагностика окружения: ключи, каталоги, плагины, харнессы, MCP.
    Doctor,
    /// Pre-flight гейт для ML-эксперимента: детерминированные проверки ДО
    /// аренды GPU (ABI-матрица, VRAM, хосты, бюджет, воспроизводимость).
    /// Читает TOML-спецификацию; `--example` печатает образец.
    Preflight {
        /// TOML-файл спецификации эксперимента.
        #[arg(value_name = "SPEC.toml")]
        spec: Option<PathBuf>,
        /// Печатать образец спецификации и выйти.
        #[arg(long)]
        example: bool,
        /// Машиночитаемый вывод: JSON.
        #[arg(long)]
        json: bool,
    },
    /// Экспорт журнала сессии в Word/Excel.
    Export {
        /// Формат: word (docx) или excel (xlsx).
        format: String,
        /// Путь к журналу сессии (session-*.jsonl).
        session: PathBuf,
        /// Куда писать файл (.docx/.xlsx).
        out: PathBuf,
    },
    /// Дельта-спецификации (propose → apply → archive).
    Delta {
        #[command(subcommand)]
        cmd: DeltaCmd,
    },
    /// Адаптер `OpenSpec`: требования openspec/ → покрытие fitness-правилами
    /// (MVP, `docs/openspec.md`).
    Openspec {
        #[command(subcommand)]
        cmd: OpenspecCmd,
    },
    /// AGENTS.md для репозиториев команд: генерация из архитектурных артефактов.
    AgentsMd {
        #[command(subcommand)]
        cmd: AgentsMdCmd,
    },
    /// Планировщик md-задач.
    Cron {
        #[command(subcommand)]
        cmd: CronCmd,
    },
    /// Регрессионные eval-сьюты конфигурации харнесса (continuous evals).
    Eval {
        #[command(subcommand)]
        cmd: EvalCmd,
    },
    /// Worktree-фабрика: изоляция агентной работы в git worktree (review/accept/drop).
    Worktree {
        #[command(subcommand)]
        cmd: WorktreeCmd,
    },
    /// Аудит флота worktree: дубли и дрейф копий спайна (модель 5.2, SSOT).
    Fleet {
        #[command(subcommand)]
        cmd: FleetCmd,
    },
    /// Обратное обследование legacy-репозитория (reverse discovery):
    /// детерминированный сканер → каркас карты обследования docs/reverse/survey.md.
    Survey {
        /// Репозиторий для обследования.
        repo: PathBuf,
        /// Каталог вывода (по умолчанию <repo>/docs/reverse).
        #[arg(long)]
        out: Option<PathBuf>,
    },
    /// `ArchUnit`-мост: JVM-гейты из `CONSTRAINTS.yaml` настоящим `ArchUnit`
    /// (ADR-039): генерация `JUnit`-теста, standalone-гейт, загрузка jar'ов.
    Archunit {
        #[command(subcommand)]
        cmd: ArchunitCmd,
    },
    /// Реестр артефактов ML-контура: манифест весов/датасетов/токенизаторов
    /// (`artifacts.yaml`) и механическая проверка против файловой системы.
    Weights {
        #[command(subcommand)]
        cmd: WeightsCmd,
    },
    /// Датасет-карточки: объявленные поля против пробелов и противоречий.
    DataCard {
        #[command(subcommand)]
        cmd: DataCardCmd,
    },
    /// Eval траекторий: метрики эпизодов (`success_rate`, `ci95`, `pass@k`).
    Trajectory {
        #[command(subcommand)]
        cmd: TrajectoryCmd,
    },
}

/// Диспетчер команд: загрузка конфигурации, переопределения TUI и вызов
/// тела команды из тематического модуля `cli/*`. Коды выхода команд
/// (`std::process::exit` внутри тел) сохранены без изменений.
pub(crate) async fn dispatch(cli: Cli) -> Result<()> {
    let cfg = Arc::new(Config::load(cli.config.as_deref()).context("загрузка конфигурации")?);

    let overrides = tui_overrides(&cli)?;
    match cli.cmd {
        None | Some(Cmd::Tui) => arch_harness::tui::run_with(cfg, overrides).await?,
        Some(Cmd::Init) => misc::cmd_init(&cfg)?,
        Some(Cmd::Run {
            prompt,
            model,
            no_stream,
            quiet,
            timeout,
            max_turns,
            think,
            goal,
        }) => {
            let code = run::cmd_run(
                &cfg,
                prompt,
                model,
                !no_stream && !quiet,
                think,
                run::RunOptions {
                    timeout_secs: timeout,
                    max_turns,
                    goal,
                },
            )
            .await?;
            // Гол-режим в пайпе: код цели — наружу (0 complete / 3 blocked /
            // 6 paused). Вне гол-режима `cmd_run` возвращает 0, поведение
            // прежнее. Коды scope-нуты на `run` (у `harness-run` своя шкала).
            if code != 0 {
                std::process::exit(code);
            }
        }
        Some(Cmd::Models) => misc::cmd_models(&cfg)?,
        Some(Cmd::Prompts { name }) => misc::cmd_prompts(&cfg, name)?,
        Some(Cmd::Memory { cmd }) => misc::cmd_memory(&cfg, cmd)?,
        Some(Cmd::Govern { cmd }) => governance::cmd_govern(&cfg, cmd)?,
        Some(Cmd::Accept { cmd }) => governance::cmd_accept(&cfg, cmd)?,
        Some(Cmd::Evolve { cmd }) => governance::cmd_evolve(&cfg, cmd)?,
        Some(Cmd::Mermaid { file }) => misc::cmd_mermaid(&file)?,
        Some(Cmd::Archify { cmd }) => archify::cmd_archify(&cfg, cmd).await?,
        Some(Cmd::Rubric { cmd }) => eval::cmd_rubric(&cfg, cmd).await?,
        Some(Cmd::Bench { cmd }) => eval::cmd_bench(&cfg, cmd).await?,
        Some(Cmd::Kb { query, limit }) => knowledge::cmd_kb(&cfg, &query, limit).await?,
        Some(Cmd::Web { cmd }) => knowledge::cmd_web(&cfg, cmd).await?,
        Some(Cmd::Mcp { cmd }) => harness::cmd_mcp(&cfg, cmd).await?,
        Some(Cmd::Handoff {
            harness,
            repo,
            task,
            spec,
            rollback,
            route,
        }) => harness::cmd_handoff(
            &cfg,
            &harness,
            &repo,
            &task,
            &spec,
            rollback.as_deref(),
            &route,
        )?,
        Some(Cmd::HarnessRun {
            harness,
            repo,
            task,
        }) => harness::cmd_harness_run(&cfg, &harness, &repo, task).await?,
        Some(Cmd::Harnesses) => harness::cmd_harnesses(&cfg),
        Some(Cmd::Control { cmd }) => control::cmd_control(&cfg, cmd)?,
        Some(Cmd::Adr { cmd }) => model::cmd_adr(cmd)?,
        Some(Cmd::Publish { cmd }) => adapters::cmd_publish(cmd)?,
        Some(Cmd::Model { cmd }) => model::cmd_model(cmd)?,
        Some(Cmd::Trace { cmd }) => model::cmd_trace(cmd)?,
        Some(Cmd::Nfr { cmd }) => model::cmd_nfr(cmd)?,
        Some(Cmd::Skills { cmd }) => knowledge::cmd_skills(&cfg, cmd)?,
        Some(Cmd::Plugins { cmd }) => knowledge::cmd_plugins(&cfg, cmd)?,
        Some(Cmd::Policy { check }) => misc::cmd_policy(&cfg, check)?,
        Some(Cmd::Evidence { cmd }) => case::cmd_evidence(cmd)?,
        Some(Cmd::Metrics { cost_report }) => misc::cmd_metrics(&cfg, cost_report)?,
        Some(Cmd::Resources { cmd }) => ml::cmd_resources(&cfg, cmd)?,
        Some(Cmd::Experiment { cmd }) => ml::cmd_experiment(&cfg, cmd)?,
        Some(Cmd::Ariadna { question }) => ml::cmd_ariadna(&cfg, &question)?,
        Some(Cmd::Doctor) => misc::cmd_doctor(&cfg),
        Some(Cmd::Preflight {
            spec,
            example,
            json,
        }) => misc::cmd_preflight(spec, example, json)?,
        Some(Cmd::Export {
            format,
            session,
            out,
        }) => misc::cmd_export(&format, &session, &out)?,
        Some(Cmd::Delta { cmd }) => case::cmd_delta(cmd)?,
        Some(Cmd::Openspec { cmd }) => adapters::cmd_openspec(cmd)?,
        Some(Cmd::AgentsMd { cmd }) => adapters::cmd_agents_md(&cfg, cmd)?,
        Some(Cmd::Cron { cmd }) => misc::cmd_cron(&cfg, cmd).await?,
        Some(Cmd::Eval { cmd }) => eval::cmd_eval(&cfg, cmd).await?,
        Some(Cmd::Worktree { cmd }) => fleet::cmd_worktree(&cfg, cmd).await?,
        Some(Cmd::Fleet { cmd }) => fleet::cmd_fleet(&cfg, cmd).await?,
        Some(Cmd::Survey { repo, out }) => case::cmd_survey(&repo, out.as_deref())?,
        Some(Cmd::Archunit { cmd }) => control::cmd_archunit(cmd).await?,
        Some(Cmd::Weights { cmd }) => ml::cmd_weights(&cfg, cmd)?,
        Some(Cmd::DataCard { cmd }) => ml::cmd_data_card(&cfg, cmd)?,
        Some(Cmd::Trajectory { cmd }) => ml::cmd_trajectory(&cfg, cmd)?,
    }
    Ok(())
}
