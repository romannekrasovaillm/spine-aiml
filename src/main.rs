//! Тонкая точка входа: парсинг аргументов → вызов lib → код возврата.

use std::io::{IsTerminal, Read, Write as _};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};

use arch_harness::agent::AgentSession;
use arch_harness::config::Config;
use arch_harness::fleet_run::ControlCommand;
use arch_harness::llm::LlmRegistry;
use arch_harness::tool::ToolContext;

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
struct Cli {
    /// Путь к config.toml (иначе ./arch-ml.toml или ~/.config/arch-ml/config.toml).
    #[arg(long, global = true)]
    config: Option<PathBuf>,

    /// Тема TUI: auto (по фону терминала), dark, light.
    #[arg(long, global = true, value_name = "dark|light|auto")]
    theme: Option<String>,

    /// Без цвета: смысл несут символы и атрибуты (NO_COLOR тоже работает).
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
enum Cmd {
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

/// Подкоманды `arch-ml weights` (реестр артефактов, `artifacts.yaml`).
#[derive(Subcommand)]
enum WeightsCmd {
    /// Объявленные артефакты манифеста (без обращения к диску).
    List {
        /// Манифест артефактов (по умолчанию `artifacts.yaml`).
        #[arg(long)]
        manifest: Option<PathBuf>,
    },
    /// Механическая проверка манифеста против файловой системы.
    Verify {
        /// Манифест артефактов (по умолчанию `artifacts.yaml`).
        #[arg(long)]
        manifest: Option<PathBuf>,
        /// Вывести отчёт в JSON вместо текста.
        #[arg(long)]
        json: bool,
    },
}

/// Подкоманды `arch-ml data-card` (датасет-карточки).
#[derive(Subcommand)]
enum DataCardCmd {
    /// Проверить карточки: объявленные поля, пробелы, противоречия.
    Check {
        /// Файл или каталог карточек (по умолчанию текущий каталог).
        #[arg(long)]
        cards: Option<PathBuf>,
        /// Путь к датасету, к которому относятся проверяемые карточки.
        /// Без флага противоречия не проверяются: существование файла по
        /// неизвестному пути не доказать, ложно-красный недопустим.
        #[arg(long)]
        dataset: Option<PathBuf>,
        /// Считать пробелы и противоречия провалом (ненулевой код возврата).
        #[arg(long)]
        strict: bool,
    },
}

/// Подкоманды `arch-ml trajectory` (eval траекторий).
#[derive(Subcommand)]
enum TrajectoryCmd {
    /// Посчитать метрики эпизодов из файла траекторий.
    Metrics {
        /// Файл траекторий (JSONL).
        #[arg(long)]
        input: PathBuf,
        /// Формат входа: `auto` | `episode-jsonl` | `session-journal` | `selfplay`.
        #[arg(long, default_value = "auto")]
        format: String,
        /// `k` для несмещённой оценки pass@k.
        #[arg(long, default_value_t = 1)]
        k: usize,
        /// Вывести метрики в JSON вместо текста.
        #[arg(long)]
        json: bool,
    },
}

/// Подкоманды `arch-ml resources` (локальные GPU, `[[gpus.local]]`).
#[derive(Subcommand)]
enum ResourcesCmd {
    /// Объявленные локальные GPU из конфига + реестр известных устройств.
    List,
    /// Живая детекция `nvidia-smi` против объявленного инвентаря.
    Check,
    /// Куда гнать модель: локальное железо ($0) первым, облако — замыкающим.
    Recommend {
        /// Параметры модели, млрд.
        #[arg(long)]
        params_b: f64,
        /// dtype весов (bf16/fp16/fp8/fp32).
        #[arg(long, default_value = "bf16")]
        dtype: String,
        /// VRAM облачного GPU для сравнения, ГБ.
        #[arg(long, default_value_t = 40.0)]
        cloud_vram: f64,
        /// Учитывать живое свободное место (локальный nvidia-smi / SSH), а не
        /// только паспортный VRAM из реестра.
        #[arg(long)]
        live: bool,
    },
}

/// Подкоманды `arch-ml experiment` (provenance, ось 3).
#[derive(Subcommand)]
enum ExperimentCmd {
    /// Записать provenance эксперимента из TOML-спеки (+ git-коммит и драйвер).
    Record {
        /// TOML-файл спецификации эксперимента.
        spec: PathBuf,
        /// Репозиторий для git-коммита (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Список записанных экспериментов.
    List,
    /// Рецепт воспроизведения + сверка git-дрейфа.
    Reproduce {
        /// Id эксперимента (из `experiment list`).
        id: String,
        /// Репозиторий для сверки git-коммита (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
}

/// Подкоманды `arch-ml archunit` (ADR-039).
#[derive(Subcommand)]
enum ArchunitCmd {
    /// Сгенерировать артефакты для JVM-репо: `ArchFitnessTest.java` (`JUnit` 5 +
    /// `ArchUnit`, встраивается в репо команды) и `archunit-rules.json` (спек).
    Gen {
        /// JVM-репозиторий.
        repo: PathBuf,
        /// Файл ограничений (по умолчанию <repo>/.arch-handoff/`CONSTRAINTS.yaml`).
        #[arg(long)]
        constraints: Option<PathBuf>,
        /// Каталог типизированной модели (для `context_boundary`; дефолт
        /// <repo>/model).
        #[arg(long)]
        model_dir: Option<PathBuf>,
        /// Каталог вывода (по умолчанию <repo>/archunit-fitness).
        #[arg(long)]
        out_dir: Option<PathBuf>,
        /// Базовый пакет для `@AnalyzeClasses` (без него — выводится из
        /// якорных пакетов спека, иначе сканируется всё `..`).
        #[arg(long)]
        base_package: Option<String>,
    },
    /// Standalone-гейт: исполнить java-правила `CONSTRAINTS.yaml` настоящим
    /// `ArchUnit` на скомпилированных классах (без правок JVM-репо).
    Check {
        /// JVM-репозиторий.
        repo: PathBuf,
        /// Файл ограничений (по умолчанию <repo>/.arch-handoff/`CONSTRAINTS.yaml`).
        #[arg(long)]
        constraints: Option<PathBuf>,
        /// Каталог типизированной модели (для `context_boundary`; дефолт
        /// <repo>/model).
        #[arg(long)]
        model_dir: Option<PathBuf>,
        /// Каталог скомпилированных классов (без него — авто-детект
        /// target/classes, build/classes/java/main, out/production, classes).
        #[arg(long)]
        classes: Option<PathBuf>,
        /// Каталог с jar'ами `ArchUnit` (без него — $`ARCHUNIT_HOME`, затем
        /// ~/.arch-ml/archunit/lib).
        #[arg(long)]
        jar_dir: Option<PathBuf>,
        /// Таймаут гейта, секунды (дефолт 300).
        #[arg(long)]
        timeout_secs: Option<u64>,
        /// Машиночитаемый вывод: JSON-отчёт.
        #[arg(long)]
        json: bool,
    },
    /// Скачать пиннутые jar'ы `ArchUnit` (archunit + slf4j) с Maven Central в
    /// кэш с проверкой SHA-256.
    Fetch {
        /// Каталог назначения (по умолчанию ~/.arch-ml/archunit/lib).
        #[arg(long)]
        jar_dir: Option<PathBuf>,
    },
}

/// Подкоманды `arch-ml memory`.
#[derive(Subcommand)]
enum MemoryCmd {
    /// Дописать заметку в конец файла памяти.
    Add {
        /// Текст заметки.
        text: String,
    },
}

/// Подкоманды `arch-ml govern` (H1.1).
#[derive(Subcommand)]
enum GovernCmd {
    /// Записать решение с именным аппрувером.
    Record {
        /// Имя аппрувера (ФИО/роль).
        #[arg(long)]
        approver: String,
        /// Версия спека (`spec_version` или хэш).
        #[arg(long)]
        spec_version: String,
        /// Артефакт (путь относительно репо).
        #[arg(long)]
        artifact: Option<String>,
        /// Текст решения.
        #[arg(long)]
        decision: String,
    },
    /// Показать журнал решений.
    Show,
    /// Проверить подотчётность артефакта (PASS/FAIL, exit 1 при FAIL).
    Verify {
        /// Артефакт (без — любая запись).
        #[arg(long)]
        artifact: Option<String>,
    },
}

/// Подкоманды `arch-ml accept` (H1.2).
#[derive(Subcommand)]
enum AcceptCmd {
    /// Записать человеко-заданные приёмочные тесты (ДО генерации).
    Set {
        /// Версия спека.
        #[arg(long)]
        spec_version: String,
        /// Тесты через запятую.
        #[arg(long)]
        tests: String,
    },
    /// Показать приёмочные тесты.
    Show,
    /// Гейт: есть ли тесты для версии спека (PASS/FAIL, exit 1 при FAIL).
    Check {
        /// Версия спека.
        #[arg(long)]
        spec_version: String,
    },
}

/// Подкоманды `arch-ml evolve` (H2.2, ADR-046 п.3).
#[derive(Subcommand)]
enum EvolveCmd {
    /// Предложить изменение доменного слоя (managed-блок) или файла ядра (file).
    Propose {
        /// Id предложения (kebab-case).
        #[arg(long)]
        id: String,
        /// Целевой файл (относительно репо).
        #[arg(long)]
        target: String,
        /// Managed-блок (id) внутри файла — только для mode=block.
        #[arg(long)]
        block_id: Option<String>,
        /// Новое тело блока (mode=block) либо содержимое целиком (mode=file).
        #[arg(long)]
        content: Option<String>,
        /// Файл с новым содержимым (альтернатива --content; для mode=file).
        #[arg(long)]
        content_file: Option<PathBuf>,
        /// Режим применения: block (managed-блок, дефолт) | file (замена целиком).
        #[arg(long, default_value = "block")]
        mode: String,
        /// Базовый SHA-256 целевого файла (mode=file; по умолчанию — с диска).
        #[arg(long)]
        base_sha256: Option<String>,
        /// Именной аппрувер.
        #[arg(long)]
        approver: String,
    },
    /// Список предложений.
    List,
    /// Коммит предложения (гейт: аппрувер; для mode=file — зелёный baseline-судья).
    Commit {
        /// Id предложения.
        #[arg(long)]
        id: String,
        /// Путь baseline-судьи (иначе `ARCH_ML_JUDGE` / `[fleet] judge_binary`).
        #[arg(long)]
        judge: Option<PathBuf>,
    },
    /// Отклонить предложение.
    Reject {
        /// Id предложения.
        #[arg(long)]
        id: String,
    },
}

/// Подкоманды `arch-ml archify`.
#[derive(Subcommand)]
enum ArchifyCmd {
    /// Проверка окружения Archify (node, CLI, рендеры пяти типов).
    Doctor,
    /// Рекомендация типа диаграммы и сценария под запрос.
    Guide {
        /// Вопрос/сценарий на естественном языке.
        query: String,
    },
    /// Валидация IR: 9 artifact checks + composition-профиль.
    Validate {
        /// Тип диаграммы: architecture|workflow|sequence|dataflow|lifecycle.
        r#type: String,
        /// Путь к JSON IR.
        path: PathBuf,
        /// Composition-профиль приёмки: standard|showcase.
        #[arg(long, default_value = "showcase")]
        quality: String,
        /// Машиночитаемый вывод: сырой JSON-receipt Archify CLI (SDK-контракт v1).
        #[arg(long)]
        json: bool,
    },
    /// Финальная приёмка: атомарная доставка HTML + SHA-256 receipt.
    Deliver {
        /// Тип диаграммы: architecture|workflow|sequence|dataflow|lifecycle.
        r#type: String,
        /// Путь к JSON IR.
        path: PathBuf,
        /// Путь к выходному HTML.
        output: PathBuf,
        /// Composition-профиль приёмки: standard|showcase.
        #[arg(long, default_value = "showcase")]
        quality: String,
        /// Машиночитаемый вывод: сырой JSON-receipt Archify CLI (SDK-контракт v1).
        #[arg(long)]
        json: bool,
    },
    /// Дельта двух architecture-снапшотов (Before/Delta/After + receipt).
    Compare {
        /// Путь к базовому architecture JSON IR.
        base: PathBuf,
        /// Путь к целевому architecture JSON IR.
        head: PathBuf,
        /// Путь к выходному delta HTML.
        output: PathBuf,
        /// Composition-профиль приёмки: standard|showcase.
        #[arg(long, default_value = "showcase")]
        quality: String,
        /// Машиночитаемый вывод: сырой JSON-receipt Archify CLI (SDK-контракт v1).
        #[arg(long)]
        json: bool,
    },
}

/// Подкоманды `arch-ml fleet`.
#[derive(Subcommand)]
enum FleetCmd {
    /// SSOT-аудит флота: точные дубли документации и дрейф копий спайна.
    /// Дрейф хотя бы одного файла (разное содержимое у владельцев одного
    /// пути; канон — majority-версия) — exit code 1 (гейт для CI).
    Audit {
        /// Каталоги-worktree (каждый с копией архитектурных файлов).
        paths: Vec<PathBuf>,
        /// Репозиторий: worktree перечисляются из `git worktree list`
        /// (добавляются к позиционным путям).
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Сузить сканирование glob'ом (повторяемый), напр. --include 'model/**'.
        /// По умолчанию — **/*.md|yaml|yml|json без .git/target/node_modules/.arch-handoff.
        #[arg(long)]
        include: Vec<String>,
        /// Формат вывода: text (таблица + топ расхождений) или json.
        #[arg(long, default_value = "text")]
        format: String,
        /// Воспроизводимый text-вывод для золотых файлов/CI: без волатильных
        /// колонок (размер на диске, возраст последнего коммита).
        #[arg(long)]
        stable: bool,
        /// Exit 1, если доля точных дублей выше порога (проценты, напр. 50).
        #[arg(long)]
        fail_on_dupes: Option<f64>,
    },
    /// Гейт мерджа результата прогона флота (worktree `arch/<run-id>`) в
    /// основную ветку — «агент не имеет пути в main», интеграцию подтверждает
    /// владелец. Без --owner-approve и без --approver печатает сводку прогона
    /// (diff stat, коммиты ветки, статус контракта из лога-evidence) и
    /// ОТКАЗЫВАЕТ мержить (exit 1). Режим гейта — [fleet] `merge_gate`
    /// ("owner" по умолчанию, "none" — без гейта).
    Merge {
        /// Run-id прогона (имя worktree без префикса arch/, напр.
        /// claude-code-20260825103000 — его сообщает `harness_run` при
        /// [fleet] `require_worktree` = true).
        run_id: String,
        /// Явное подтверждение владельца: выполнить merge в основную ветку.
        /// Это НЕ именной обход: без --approver вердикт ревьювера `NOT-READY`
        /// блокирует и этот путь (ADR-046, п. 2).
        #[arg(long)]
        owner_approve: bool,
        /// Именной аппрувер: обход блокирующего вердикта ревьювера (NOT-READY)
        /// по ветке прогона. Решение пишется в журнал приёмки
        /// (`state_dir/accept/decisions.jsonl`) — той же записью, что у
        /// `worktree accept --approver`; подтверждает интеграцию вместо
        /// --owner-approve.
        #[arg(long)]
        approver: Option<String>,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Прогнать флот харнессов: план (паттерн, ADR-042) ИЛИ legacy
    /// items-файл (одна волна). Items — JSON-файл: массив
    /// {id, spec, `required_skills`[], tags[], route, domain, effort}.
    Run {
        /// Корень репозитория (с handoff-пакетом).
        #[arg(long)]
        repo: PathBuf,
        /// JSON-файл с work-items (массив объектов) — legacy-веер.
        #[arg(long, conflicts_with = "plan")]
        items_file: Option<PathBuf>,
        /// Файл плана флота (.arch-fleet/<id>.plan.toml|json|yaml).
        #[arg(long)]
        plan: Option<PathBuf>,
        /// Метка пакета/эпика (для журнала).
        #[arg(long, default_value = "fleet")]
        package: String,
        /// Baseline-судья гейтов узлов (ADR-046): путь к бинарю, собранному
        /// ДО прогона. Приоритет над `[fleet] judge_binary`. Без флага и
        /// конфига берётся бинарь, запустивший флот; команда гейта, зовущая
        /// судью внутри ворктри узла, без baseline падает (самооценка).
        #[arg(long)]
        judge: Option<PathBuf>,
    },
    /// Управление планами флота: предложить, проверить, показать.
    Plan {
        #[command(subcommand)]
        cmd: FleetPlanCmd,
    },
    /// Продолжить прогон плана из журнала событий (ADR-042): завершённые
    /// узлы пропускаются, незавершённые догоняются.
    Resume {
        /// Run-id прогона плана (fpl-…).
        run_id: String,
        /// Корень репозитория.
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Разрешить повтор узлов без worktree-изоляции (неидемпотентно).
        #[arg(long)]
        force_rerun: bool,
    },
    /// Показать dashboard прогона флота: карта назначений + статусы агентов
    /// из журнала state/fleet/<run-id>.jsonl.
    Status {
        /// Run-id прогона (или "latest" — последний журнал в state/fleet/).
        run_id: String,
        /// Машиночитаемая сводка (JSON) вместо текстового dashboard.
        #[arg(long)]
        json: bool,
    },
    /// Отправить команду управления в control-канал прогона.
    Control {
        /// Run-id прогона.
        run_id: String,
        /// Агент (или "*" — все).
        #[arg(long)]
        agent: String,
        /// Убить агента (Kill).
        #[arg(long)]
        kill: bool,
        /// Поставить на паузу (SIGSTOP).
        #[arg(long)]
        pause: bool,
        /// Снять паузу (SIGCONT).
        #[arg(long)]
        resume: bool,
        /// Перекинуть item другому агенту (payload — новый агент).
        #[arg(long)]
        reroute: Option<String>,
        /// Поднять приоритет (payload — число; no-op в конкурентной модели).
        #[arg(long)]
        priority: Option<usize>,
    },
}

/// Подкоманды `arch-ml fleet plan`.
#[derive(Subcommand)]
enum FleetPlanCmd {
    /// Предложить черновик плана по handoff-пакету репозитория (паттерн по
    /// маршруту значимости). Пишет `<repo>/.arch-fleet/<id>.plan.toml`;
    /// автозапуска нет — план утверждает владелец.
    Propose {
        /// Корень репозитория с `.arch-handoff/MANIFEST.json`.
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Форсировать паттерн (иначе — по маршруту пакета).
        #[arg(long)]
        pattern: Option<String>,
        /// Каталог планов (дефолт `[fleet] plan_dir` = .arch-fleet).
        #[arg(long)]
        out: Option<PathBuf>,
        /// Собрать план из набора решений (ADR-045) вместо handoff-пакета:
        /// один узел на ADR, рёбра — из `depends_on`.
        #[arg(long)]
        from_adrs: bool,
        /// Каталог ADR для `--from-adrs` (дефолт `docs/adr` от `--repo`).
        #[arg(long)]
        adr_dir: Option<PathBuf>,
        /// Отбор ADR по статусу: proposed|accepted|all (дефолт proposed).
        #[arg(long)]
        status: Option<String>,
    },
    /// Механическая проверка плана (без LLM, без прогона): exit 1 при ошибке.
    Validate {
        /// Файл плана (.toml|.json|.yaml).
        plan: PathBuf,
        /// Машиночитаемый вывод (JSON).
        #[arg(long)]
        json: bool,
    },
    /// Показать скомпилированный план: волны, роли, гейты, зависимости.
    Show {
        /// Файл плана (.toml|.json|.yaml).
        plan: PathBuf,
        /// Диаграмма Mermaid вместо таблицы волн.
        #[arg(long)]
        mermaid: bool,
        /// Машиночитаемый вывод (JSON).
        #[arg(long)]
        json: bool,
    },
}

/// Подкоманды `arch-ml worktree`.
#[derive(Subcommand)]
enum WorktreeCmd {
    /// Создать изолированный worktree (ветка arch/<name>).
    New {
        /// Имя (kebab-case [a-z0-9-]).
        name: String,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Базовая ветка/коммит (по умолчанию HEAD).
        #[arg(long)]
        base: Option<String>,
    },
    /// Список worktree фабрики.
    List {
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Diff ветки worktree против HEAD (review).
    Diff {
        /// Имя worktree.
        name: String,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Принять: merge в текущую ветку + уборка worktree.
    Accept {
        /// Имя worktree.
        name: String,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Явный именной аппрувер: обход блокирующего вердикта ревьювера
        /// (NOT-READY) по этой ветке. Решение пишется в журнал приёмки
        /// (`state_dir/accept/decisions.jsonl`).
        #[arg(long)]
        approver: Option<String>,
    },
    /// Удалить worktree без merge (только чистое).
    Drop {
        /// Имя worktree.
        name: String,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
}

#[derive(Subcommand)]
enum SkillsCmd {
    /// Список всех скиллов библиотеки.
    List,
    /// Поиск по скиллам.
    Search {
        /// Запрос.
        query: String,
        /// Максимум результатов.
        #[arg(long, default_value_t = 8)]
        limit: usize,
    },
    /// Показать полный текст скилла.
    Show {
        /// Точное имя скилла.
        name: String,
    },
}

#[derive(Subcommand)]
enum PluginsCmd {
    /// Список плагинов.
    List,
    /// Подробности плагина (манифест, скиллы, MCP-серверы).
    Show {
        /// Имя плагина.
        name: String,
    },
}

#[derive(Subcommand)]
enum RubricCmd {
    /// Список якорных рубрик.
    List,
    /// Оценить файл по рубрике (LLM-судья).
    Run {
        /// Рубрика (имя файла в assets/rubrics или путь).
        rubric: String,
        /// Целевой документ (md/txt).
        target: PathBuf,
        /// Модель-судья.
        #[arg(long)]
        model: Option<String>,
        /// Сначала сгенерировать динамическую рубрику под предмет.
        #[arg(long)]
        dynamic_subject: Option<String>,
    },
}

#[derive(Subcommand)]
enum BenchCmd {
    /// Список бенчмарков.
    List,
    /// Прогнать бенчмарк.
    Run {
        /// Имя файла бенчмарка в assets/benchmarks (или путь).
        name: Option<String>,
        /// Испытуемая модель (для --golden — модель-судья).
        #[arg(long)]
        model: Option<String>,
        /// Прогон судьи по golden-set (assets/benchmarks/golden): метрика
        /// согласия с эталоном MAE; выше порога `judge.golden_max_mae` — exit 1.
        #[arg(long)]
        golden: bool,
        /// Дописать результат golden-прогона строкой JSON в evidence-журнал
        /// (M-2, история — `bench golden-history`).
        #[arg(long)]
        record: Option<PathBuf>,
    },
    /// История golden-прогонов из evidence-журнала (JSONL) — markdown-таблица.
    GoldenHistory {
        /// Путь к журналу, записанному `bench run --golden --record`.
        path: PathBuf,
    },
    /// Согласие golden-эталонов с оценками живых архитекторов (J-3,
    /// протокол — docs/judge-human-agreement.md).
    HumanAgreement {
        /// Каталог golden-set (`<имя>.md` + `<имя>.expected.yaml`).
        #[arg(long)]
        golden_dir: PathBuf,
        /// Каталог человеческих анкет (`<документ>.<участник>.expected.yaml`).
        #[arg(long)]
        humans: PathBuf,
    },
}

#[derive(Subcommand)]
enum WebCmd {
    /// Поиск в вебе.
    Search {
        /// Запрос.
        query: String,
        /// Ограничить кураторскими архитектурными сайтами.
        #[arg(long)]
        arch: bool,
    },
    /// Загрузить страницу текстом.
    Fetch {
        /// URL.
        url: String,
    },
    /// Кураторский список сайтов архитектора.
    Sites,
}

#[derive(Subcommand)]
enum McpCmd {
    /// Список серверов и их инструментов.
    List,
    /// Вызвать MCP-инструмент.
    Call {
        /// Составное имя `server__tool`.
        name: String,
        /// Аргументы JSON.
        #[arg(default_value = "{}")]
        args: String,
    },
    /// MCP-сервер (stdio JSON-RPC, NDJSON): архитектурный контроль кодовым
    /// агентам (Claude Code и др.) — `spine_lint`, `fitness_check`,
    /// `significance_score`, `trace_check`, `model_query`, `rubric_run` (ADR-008).
    Serve,
}

#[derive(Subcommand)]
enum ControlCmd {
    /// Fitness-контроль репозитория по `CONSTRAINTS.yaml`.
    Check {
        /// Репозиторий.
        repo: PathBuf,
        /// Файл ограничений (по умолчанию — рабочий <repo>/`CONSTRAINTS.yaml`,
        /// затем пакетный <repo>/.arch-handoff/`CONSTRAINTS.yaml` как fallback
        /// с предупреждением).
        #[arg(long)]
        constraints: Option<PathBuf>,
        /// Машиночитаемый вывод: JSON-отчёт `FitnessReport` (SDK-контракт v1).
        #[arg(long)]
        json: bool,
    },
    /// Линтер ARCHITECTURE-SPINE.md.
    Spine {
        /// Путь к spine-файлу.
        file: PathBuf,
    },
    /// Сенсоры спецификаций (required-sections, upstream-coverage).
    Sensors {
        /// Каталог спецификаций.
        dir: PathBuf,
    },
    /// Architecture Significance Score: `--trigger new_component=true ...`
    Score {
        /// Триггеры вида имя=true/false.
        #[arg(long)]
        trigger: Vec<String>,
        /// Anti-bypass floor (ADR-034): механически вывести триггеры из
        /// git-диффа и объединить с заявленными (fail-safe — детектор только
        /// добавляет). Без значения — рабочее дерево против HEAD; со
        /// значением — `git diff GIT_REF...HEAD`.
        #[arg(long, num_args = 0..=1, default_missing_value = "HEAD", value_name = "GIT_REF")]
        from_diff: Option<String>,
    },
    /// Отчёт по реестру правил `CONSTRAINTS.yaml` (сводка, таблица карточек,
    /// находки: без owner/expiry, просроченные, `exclude_glob`, git-прокси
    /// стоимости сопровождения, суммарный `effort_hours`).
    RulesReport {
        /// Репозиторий.
        repo: PathBuf,
        /// Файл ограничений (по умолчанию — рабочий <repo>/`CONSTRAINTS.yaml`,
        /// затем пакетный <repo>/.arch-handoff/`CONSTRAINTS.yaml` как fallback).
        #[arg(long)]
        constraints: Option<PathBuf>,
    },
    /// Отчёт вверх по корпоративному контуру (наследование `extends`,
    /// `docs/corp-spine.md`): покрытие корп-правил, исходы (pass/fail/warn),
    /// overrides со статусами, просроченные правила, расхождения пинов версий.
    Report {
        /// Репозиторий.
        repo: PathBuf,
        /// Файл ограничений (по умолчанию — рабочий <repo>/`CONSTRAINTS.yaml`,
        /// затем пакетный <repo>/.arch-handoff/`CONSTRAINTS.yaml` как fallback).
        #[arg(long)]
        constraints: Option<PathBuf>,
        /// Уровень: corp (только унаследованные правила) | all (все).
        #[arg(long, default_value = "corp")]
        level: String,
        /// Машиночитаемый вывод: JSON-отчёт `ControlReport` (SDK-контракт v1).
        #[arg(long)]
        json: bool,
    },
    /// Новый ADR.
    Adr {
        /// Заголовок решения.
        title: String,
        /// Каталог ADR (по умолчанию ./docs/adr).
        #[arg(long)]
        dir: Option<PathBuf>,
    },
    /// Гейт контрольной точки (пока A4 — conformance evidence: репетиция
    /// отката handoff-пакета, см. docs/control.md).
    Gate {
        /// Идентификатор гейта (реализован только A4).
        gate: String,
        /// Репозиторий (с .arch-handoff/) или каталог handoff-пакета.
        packet: PathBuf,
        /// Перед оценкой гейта прогнать репетицию отката (обновляет
        /// .arch-handoff/REHEARSAL.json).
        #[arg(long)]
        rehearse: bool,
        /// Репетиция обязательна для маршрутов не ниже порога:
        /// fast|standard|critical|never (дефолт critical).
        #[arg(long, default_value = "critical")]
        require_rehearsal: String,
    },
}

/// Подкоманды `arch-ml adr` (реестр ADR, ADR-036).
#[derive(Subcommand)]
enum AdrCmd {
    /// Глобальный реестр ADR по набору проектов: сам ROOT + непосредственные
    /// подкаталоги; источники — docs/adr/*.md (проза) и model/ADR-*.md
    /// (типизированные сущности ADR-003).
    Registry {
        /// Корневой каталог набора проектов.
        root: PathBuf,
        /// Машиночитаемый вывод: единый JSON {entries, findings}.
        #[arg(long)]
        json: bool,
        /// Exit 1 при любой находке (коллизия номеров, дубль заголовка,
        /// пропуск даты/статуса) — гейт для CI; по умолчанию exit 0.
        #[arg(long)]
        strict: bool,
    },
}

/// Подкоманды `arch-ml publish` (файловые адаптеры, ADR-033).
#[derive(Subcommand)]
enum PublishCmd {
    /// Markdown → Confluence storage format (XHTML) в stdout: заголовки,
    /// таблицы, код-блоки, списки, инлайн-разметка (подмножество).
    Confluence {
        /// Markdown-файл (spine, ADR, evidence-индекс).
        file: PathBuf,
    },
    /// JSON результата handoff → Jira-CSV импорта (Summary,Type,Description,Labels).
    Jira {
        /// Файл результата handoff (`status`/`assumptions`/`open_questions`/…).
        result: PathBuf,
        /// Ключ проекта Jira — метка `spine-<ключ>` для фильтрации.
        #[arg(long)]
        project: Option<String>,
    },
}

/// Подкоманды `arch-ml model` (ADR-003).
#[derive(Subcommand)]
enum ModelCmd {
    /// Ссылочная целостность модели: битая ссылка/дубль ID/цикл `depends_on` —
    /// error (exit code 1, как у `control check`); ADR без CMP, NFR без
    /// способа проверки — warn.
    Validate {
        /// Каталог модели.
        dir: PathBuf,
    },
    /// Карточка сущности: шапка, связи, обратные ссылки, тело.
    Show {
        /// ID сущности (ADR-001, CMP-002, …).
        id: String,
        /// Каталог модели (по умолчанию ./model).
        #[arg(long, default_value = "model")]
        dir: PathBuf,
    },
    /// Граф связей модели.
    Graph {
        /// Каталог модели (по умолчанию ./model).
        #[arg(long, default_value = "model")]
        dir: PathBuf,
        /// Формат: text (список) или mermaid (flowchart, совместим с `arch-ml mermaid`).
        #[arg(long, default_value = "text")]
        format: String,
    },
    /// Проекция: рендер ADR-файлов из модели в <кейс>/.arch-handoff/adr/
    /// (зеркально; устаревшие ADR-*.md удаляются).
    Project {
        /// Каталог модели.
        dir: PathBuf,
    },
    /// Экспорт модели в отраслевой формат (ADR-009, ADR-032): Structurizr
    /// DSL, `PlantUML`, drawio (SYS/CMP/INT + связи) или `ArchiMate` Open
    /// Exchange 3.2 (SYS/CMP/INT/CAP/REQ/NFR/AD + связи) — на stdout.
    Export {
        /// Каталог модели.
        dir: PathBuf,
        /// Формат: structurizr, plantuml, drawio или archimate.
        #[arg(long)]
        format: String,
    },
    /// Импорт Structurizr DSL в модель: сущности SYS/CMP/INT + связи
    /// (по одному .md на элемент; существующие файлы не затираются).
    Import {
        /// Файл Structurizr DSL.
        file: PathBuf,
        /// Формат (пока только structurizr).
        #[arg(long)]
        format: String,
        /// Каталог модели-получателя (создаётся при отсутствии).
        #[arg(long, default_value = "model")]
        dir: PathBuf,
    },
    /// Ландшафт систем набора проектов (EA-3, ADR-036/ADR-037): агрегация
    /// `model/` самого ROOT и непосредственных подкаталогов в единый
    /// реестр систем SYS/INT с дедупликацией по имени (без глобальных ID),
    /// находки (id-divergence, status-conflict, dangling-ref,
    /// cross-project-link) и топ связности.
    Landscape {
        /// Корневой каталог набора проектов.
        root: PathBuf,
        /// Дополнительно вывести mermaid `graph TD` ландшафта.
        #[arg(long)]
        mermaid: bool,
    },
}

/// Подкоманды `arch-ml trace` (ADR-006).
#[derive(Subcommand)]
enum TraceCmd {
    /// Позвенная трассируемость: REQ → NFR → AD/ADR → CMP → правило
    /// `CONSTRAINTS.yaml`; AD без правила и без `unverifiable` — error
    /// (exit code 1). Отчёт markdown, пригоден для evidence bundle.
    Check {
        /// Корень кейса (каталог с model/).
        dir: PathBuf,
    },
}

/// Подкоманды `arch-ml nfr` (ADR-007).
#[derive(Subcommand)]
enum NfrCmd {
    /// Latency-бюджет: сумма бюджетов hop'ов INT-* против цели p99 из NFR-*;
    /// hop без бюджета или превышение — error (exit code 1).
    Budget {
        /// Корень кейса (каталог с model/).
        dir: PathBuf,
    },
    /// Доступность: композиция последовательных/параллельных участков против
    /// SLA из NFR-* + цели RTO/RPO; ниже SLA — error (exit code 1).
    Availability {
        /// Корень кейса (каталог с model/).
        dir: PathBuf,
    },
    /// Пропускная способность: RPS-цель против ёмкости компонентов
    /// (instances × `rps_per_instance`); дефицит — error (exit code 1).
    Capacity {
        /// Корень кейса (каталог с model/).
        dir: PathBuf,
    },
    /// Стоимость: TCO (инстансы × тариф) и цена выхода (Σ `exit_cost`)
    /// по тарифным данным сущностей.
    Cost {
        /// Корень кейса (каталог с model/).
        dir: PathBuf,
    },
}

#[derive(Subcommand)]
enum EvidenceCmd {
    /// Собрать bundle (EVIDENCE.yaml) по каталогу изменения.
    Pack {
        /// Каталог изменения.
        dir: PathBuf,
        /// Маршрут: fast|standard|critical.
        #[arg(long, default_value = "standard")]
        route: String,
    },
    /// Проверить bundle: полнота + целостность хэшей.
    Verify {
        /// Каталог изменения.
        dir: PathBuf,
    },
}

#[derive(Subcommand)]
enum DeltaCmd {
    /// Новая дельта (каркас changes/<name>/DELTA.md).
    New {
        /// Имя изменения (kebab-case).
        name: String,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Список дельт (предложенные/архивные).
    List {
        /// Репозиторий.
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Валидация структуры дельты.
    Validate {
        /// Имя дельты.
        name: String,
        /// Репозиторий.
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Архивировать дельту после apply (вливание в живую истину).
    Archive {
        /// Имя дельты.
        name: String,
        /// Репозиторий.
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Гейт прямых правок спайна: изменённые защищённые файлы обязаны
    /// упоминаться в активной дельте changes/<name>/DELTA.md, иначе exit 1.
    /// Новые untracked-файлы git-diff не видит — для CI используйте --base.
    Guard {
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
        /// База diff (по умолчанию HEAD — staged+unstaged рабочего дерева;
        /// для CI — напр. origin/main...HEAD: трёхточечную форму разбирает
        /// сам git).
        #[arg(long)]
        base: Option<String>,
        /// Защищаемый путь/префикс (повторяемый). Если задан хотя бы один —
        /// заменяет дефолт: model/, ARCHITECTURE-SPINE.md, `CONSTRAINTS.yaml`.
        #[arg(long)]
        protect: Vec<String>,
    },
}

/// Подкоманды `arch-ml openspec` (адаптер `OpenSpec`, MVP; `docs/openspec.md`).
#[derive(Subcommand)]
enum OpenspecCmd {
    /// Список требований `OpenSpec`: живые спеки (openspec/specs/) и дельты
    /// активных changes; стабильный id `openspec:<capability>#<hash8>`,
    /// текст, источник (файл:строка).
    Scan {
        /// Корень репозитория с разметкой `OpenSpec`.
        root: PathBuf,
        /// Машиночитаемый вывод: JSON-отчёт `ScanReport`.
        #[arg(long)]
        json: bool,
    },
    /// Отчёт покрытия требований правилами CONSTRAINTS (связь — поле
    /// `covers:` правила): SHALL всего / покрыто детектором / unverifiable
    /// с owner / без решения; непокрытые — поимённо. Exit code: 0, если нет
    /// --strict; с --strict — 1 при наличии требований «без решения»
    /// (ни детектора, ни unverifiable с назначенным owner).
    Coverage {
        /// Корень репозитория с разметкой `OpenSpec`.
        root: PathBuf,
        /// Файл ограничений (по умолчанию <root>/.arch-handoff/CONSTRAINTS.yaml,
        /// иначе <root>/CONSTRAINTS.yaml; нет файла — все «без решения»).
        #[arg(long)]
        constraints: Option<PathBuf>,
        /// Машиночитаемый вывод: JSON-отчёт `CoverageReport`.
        #[arg(long)]
        json: bool,
        /// Строгий режим: exit 1 при требованиях «без решения» (гейт CI).
        #[arg(long)]
        strict: bool,
    },
    /// Генерация артефактов перехода `OpenSpec` → Spine: скелет
    /// CONSTRAINTS.from-openspec.yaml (все SHALL как заглушки
    /// `unverifiable: true` с пустым owner и проставленным `covers:`),
    /// SPINE.draft.md (кандидаты из design.md активных changes) и печать
    /// отчёта покрытия. Существующие файлы не затираются без --force.
    Init {
        /// Корень репозитория с разметкой `OpenSpec`.
        root: PathBuf,
        /// Каталог вывода (по умолчанию — сам ROOT).
        #[arg(long)]
        out: Option<PathBuf>,
        /// Перезаписать существующие файлы (регенерация детерминирована).
        #[arg(long)]
        force: bool,
    },
    /// Гейт архивации change (точка CI перед `openspec archive`): exit 1,
    /// если у требований change нет решения (ни детектора, ни unverifiable
    /// с owner) или падает `control check`. Реализован только --archive
    /// (roadmap: --change, --expiry — `docs/openspec.md`).
    Gate {
        /// Режим гейта: архивация change.
        #[arg(long)]
        archive: bool,
        /// Корень репозитория с разметкой `OpenSpec`.
        root: PathBuf,
        /// Идентификатор change (каталог openspec/changes/<id>).
        change_id: String,
        /// Файл ограничений (умолчание — как у `coverage`).
        #[arg(long)]
        constraints: Option<PathBuf>,
    },
}

#[derive(Subcommand)]
enum AgentsMdCmd {
    /// Сгенерировать или обновить AGENTS.md (рукописная зона сохраняется).
    Refresh {
        /// Репозиторий.
        repo: PathBuf,
    },
    /// Проверить AGENTS.md: свежесть (дрейф источников), ссылки, заглушки.
    Lint {
        /// Репозиторий.
        repo: PathBuf,
    },
    /// Прогнать линтер по реестру репозиториев (файл: путь на строку).
    LintAll {
        /// Файл реестра (по умолчанию ~/.arch-ml/repos.txt).
        #[arg(long)]
        registry: Option<PathBuf>,
    },
}

#[derive(Subcommand)]
enum CronCmd {
    /// Список задач расписания.
    List,
    /// Запустить задачу по имени сейчас.
    Run {
        /// Имя задачи.
        name: String,
    },
    /// Проверить и запустить дюжные задачи (для системного cron).
    Tick,
}

/// Подкоманды `arch eval` (continuous evals, docs/evals.md).
#[derive(Subcommand)]
enum EvalCmd {
    /// Прогнать eval-сьют: детерминированные проверки (офлайн) + опциональный
    /// LLM-судья. Pass-rate ниже гейта — exit code 1 (регрессионный гейт).
    Run {
        /// Каталог сьюта (YAML-задачи). Без флага — встроенный сьют
        /// agent-config, прогоняемый герметично (ассеты разворачиваются во
        /// временный каталог; живой конфиг не трогается).
        #[arg(long)]
        suite: Option<PathBuf>,
        /// Гейт pass-rate в процентах (дефолт 100): ниже — exit code 1.
        #[arg(long)]
        gate: Option<f64>,
        /// Включить слой LLM-судьи (prompt-задачи и рубрики; нужен API-ключ).
        #[arg(long)]
        judge: bool,
        /// Модель для слоя судьи (имя из [models]; иначе — default).
        #[arg(long)]
        model: Option<String>,
    },
}

/// Печать в stdout с юниксовой семантикой пайпа: читатель, закрывший пайп
/// раньше (`| head`, `| less -F`), — тихий выход с кодом 0, а не паника
/// «Broken pipe» (std ставит SIGPIPE в ignore, поэтому `outln!` паникует;
/// `unsafe` запрещён инвариантом AD-6, поэтому ловим ошибку записи).
fn out(args: std::fmt::Arguments) {
    use std::io::Write as _;
    if let Err(e) = std::io::stdout().lock().write_fmt(args) {
        if e.kind() == std::io::ErrorKind::BrokenPipe {
            std::process::exit(0);
        }
        // Прочие ошибки вывода — как у outln!: паника с причиной.
        panic!("failed printing to stdout: {e}");
    }
}

/// `outln!`, стойкий к закрытому пайпу (см. [`out`]).
macro_rules! outln {
    () => { out(format_args!("\n")) };
    ($($arg:tt)*) => { out(format_args!("{}\n", format_args!($($arg)*))) };
}

/// `print!`, стойкий к закрытому пайпу (см. [`out`]).
macro_rules! outp {
    ($($arg:tt)*) => { out(format_args!($($arg)*)) };
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn")),
        )
        .with_writer(std::io::stderr)
        .init();

    let cli = Cli::parse();
    let cfg = Arc::new(Config::load(cli.config.as_deref()).context("загрузка конфигурации")?);

    let overrides = tui_overrides(&cli)?;
    match cli.cmd {
        None | Some(Cmd::Tui) => arch_harness::tui::run_with(cfg, overrides).await?,
        Some(Cmd::Init) => cmd_init(&cfg)?,
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
            let code = cmd_run(
                &cfg,
                prompt,
                model,
                !no_stream && !quiet,
                think,
                RunOptions {
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
        Some(Cmd::Models) => {
            let registry = LlmRegistry::from_config(&cfg)?;
            outln!("Модели (по умолчанию: {}):", registry.default_name());
            for name in registry.names() {
                let p = registry.get(&name)?;
                outln!("  {name:<20} {} ({})", p.model(), p.name());
            }
        }
        Some(Cmd::Prompts { name }) => cmd_prompts(&cfg, name)?,
        Some(Cmd::Memory { cmd }) => cmd_memory(&cfg, cmd)?,
        Some(Cmd::Govern { cmd }) => cmd_govern(&cfg, cmd)?,
        Some(Cmd::Accept { cmd }) => cmd_accept(&cfg, cmd)?,
        Some(Cmd::Evolve { cmd }) => cmd_evolve(&cfg, cmd)?,
        Some(Cmd::Mermaid { file }) => {
            // Каталог — понятная подсказка со списком *.mmd, а не «os error 21».
            let input = if file != "-" && std::path::Path::new(&file).is_dir() {
                arch_harness::mermaid::read_diagram_source(std::path::Path::new(&file))?
            } else {
                read_file_or_stdin(&file)?
            };
            let art = arch_harness::mermaid::render(&input)?;
            outln!("{art}");
        }
        Some(Cmd::Archify { cmd }) => cmd_archify(&cfg, cmd).await?,
        Some(Cmd::Rubric { cmd }) => cmd_rubric(&cfg, cmd).await?,
        Some(Cmd::Bench { cmd }) => cmd_bench(&cfg, cmd).await?,
        Some(Cmd::Kb { query, limit }) => {
            let hits = arch_harness::kb::search(
                &cfg.knowledge.dirs,
                &cfg.knowledge.extensions,
                &query,
                limit,
            )
            .await?;
            for hit in &hits {
                outln!(
                    "── {}:{} (score {:.1})",
                    hit.path.display(),
                    hit.line,
                    hit.score
                );
                outln!("{}", hit.snippet);
            }
            if hits.is_empty() {
                outln!("Ничего не найдено.");
            }
        }
        Some(Cmd::Web { cmd }) => cmd_web(&cfg, cmd).await?,
        Some(Cmd::Mcp { cmd }) => cmd_mcp(&cfg, cmd).await?,
        Some(Cmd::Handoff {
            harness,
            repo,
            task,
            spec,
            rollback,
            route,
        }) => {
            if !cfg.harnesses.contains_key(&harness) {
                anyhow::bail!(
                    "неизвестный харнесс '{harness}'. Известные: {:?}",
                    arch_harness::harness::known()
                );
            }
            let route: arch_harness::control::Route =
                route.parse().map_err(|e: String| anyhow::anyhow!(e))?;
            let packet = arch_harness::harness::generate_handoff(
                &repo,
                &task,
                &spec,
                &cfg,
                rollback.as_deref(),
                route,
            )?;
            outln!("Handoff-пакет: {}", packet.dir.display());
            for f in &packet.files {
                outln!("  {}", f.display());
            }
            outln!("epic-context ≈ {} токенов", packet.epic_context_tokens);
            match &packet.baseline {
                Some(h) => outln!(
                    "git: {}baseline {h} (якорь отката)",
                    if packet.git_initialized {
                        "инициализирован, "
                    } else {
                        ""
                    }
                ),
                None => outln!("⚠ git недоступен — якоря отката нет"),
            }
            outln!(
                "маршрут {route} → рекомендованный timeout_secs={}",
                packet.recommended_timeout_secs
            );
            if packet.git_dirty_tracked {
                outln!(
                    "⚠ незакоммиченные изменения отслеживаемых файлов: откат на baseline их потеряет"
                );
            }
        }
        Some(Cmd::HarnessRun {
            harness,
            repo,
            task,
        }) => {
            use arch_harness::harness::Termination;
            let hcfg = cfg
                .harnesses
                .get(&harness)
                .with_context(|| format!("харнесс '{harness}' не настроен"))?;
            // [fleet] require_worktree: прогон изолируется в git worktree
            // (ветка arch/<run-id>), основное дерево не трогается; мерж —
            // только гейтом владельца (`arch fleet merge <run-id>`).
            let (repo, run_id) =
                match arch_harness::harness::enforce_run_worktree(&cfg, &repo, &harness).await? {
                    Some((dir, run_id)) => {
                        outln!(
                            "⚑ [fleet] require_worktree: прогон изолирован в worktree \
                             arch/{run_id} ({}); основное дерево не изменяется",
                            dir.display()
                        );
                        outln!(
                            "  интеграция — гейт владельца: arch fleet merge {run_id} \
                             [--owner-approve]; отклонение: arch worktree drop {run_id}"
                        );
                        (dir, Some(run_id))
                    }
                    None => (repo, None),
                };
            let task_text = match task {
                Some(t) => t,
                None => std::fs::read_to_string(repo.join(".arch-handoff/TASK.md"))
                    .context("нет --task и не найден .arch-handoff/TASK.md")?,
            };
            let mut hcfg_owned = hcfg.clone();
            if let Some(t) = arch_harness::harness::recommended_timeout_secs(&repo) {
                // Пакет несёт рекомендацию по маршруту значимости (Fast/Standard/Critical).
                hcfg_owned.timeout_secs = t.clamp(600, 7200);
            }
            let run = arch_harness::harness::run_harness(&harness, &hcfg_owned, &repo, &task_text)
                .await?;
            if let Some(ac) = &run.auto_commit {
                outln!(
                    "⚑ авто-коммит: исполнитель не зафиксировал результат — {} путей → {} «{}»",
                    ac.files,
                    ac.hash,
                    ac.message
                );
            }
            match &run.contract {
                arch_harness::harness::ContractParse::Valid(c) => {
                    outln!(
                        "контракт: status={} assumptions={} open_questions={} conflicts={}",
                        c.status.as_str(),
                        c.assumptions.len(),
                        c.open_questions.len(),
                        c.conflicts.len()
                    );
                }
                arch_harness::harness::ContractParse::Invalid(r) => {
                    eprintln!("⚠ контракт найден, но невалиден по схеме: {r}");
                }
                arch_harness::harness::ContractParse::Missing => {
                    eprintln!("⚠ контракт результата (```json со status) в stdout не найден");
                }
            }
            if let Some(id) = &run_id {
                outln!(
                    "прогон изолирован в worktree arch/{id}: мерж — arch fleet merge {id} \
                     --owner-approve, отклонение — arch worktree drop {id}"
                );
            }
            if run.termination != Termination::Completed {
                eprintln!(
                    "⚠ прогон ПРЕРВАН ({}{}); процессная группа завершена, \
                     репозиторий может быть в промежуточном состоянии — проверьте git status",
                    run.termination,
                    if run.termination == Termination::IdleTimeout {
                        format!(" {} с", hcfg.idle_timeout_secs)
                    } else {
                        format!(" {} с", hcfg.timeout_secs)
                    }
                );
            }
            outln!(
                "── stdout (exit {:?}, {:.1}s) ──",
                run.exit_code,
                run.duration_secs
            );
            outln!("{}", run.stdout);
            if !run.stderr.is_empty() {
                eprintln!("── stderr ──\n{}", run.stderr);
            }
            // Скриптовый гейт: status=blocked — код 2; непустые
            // conflicts_with_prior_decisions — код 3 (конфликт со spine
            // останавливает интеграцию по контракту). Полная схема кодов —
            // docs/harness_integrations.md.
            if let arch_harness::harness::ContractParse::Valid(c) = &run.contract {
                if c.status == arch_harness::harness::ContractStatus::Blocked {
                    std::process::exit(2);
                }
                if !c.conflicts.is_empty() {
                    std::process::exit(3);
                }
            }
        }
        Some(Cmd::Harnesses) => {
            outln!("Известные кодовые харнессы:");
            for name in arch_harness::harness::known() {
                let status = match cfg.harnesses.get(name) {
                    Some(h) => format!("{} ({:?})", h.binary, h.prompt_mode),
                    None => "не настроен".into(),
                };
                let installed = which(cfg.harnesses.get(name).map_or(name, |h| h.binary.as_str()));
                outln!("  {name:<14} {status:<40} {installed}");
            }
        }
        Some(Cmd::Control { cmd }) => cmd_control(&cfg, cmd)?,
        Some(Cmd::Adr { cmd }) => cmd_adr(cmd)?,
        Some(Cmd::Publish { cmd }) => cmd_publish(cmd)?,
        Some(Cmd::Model { cmd }) => cmd_model(cmd)?,
        Some(Cmd::Trace { cmd }) => cmd_trace(cmd)?,
        Some(Cmd::Nfr { cmd }) => cmd_nfr(cmd)?,
        Some(Cmd::Skills { cmd }) => cmd_skills(&cfg, cmd)?,
        Some(Cmd::Plugins { cmd }) => cmd_plugins(&cfg, cmd)?,
        Some(Cmd::Policy { check }) => cmd_policy(&cfg, check)?,
        Some(Cmd::Evidence { cmd }) => cmd_evidence(cmd)?,
        Some(Cmd::Metrics { cost_report }) => {
            if cost_report {
                // Смета по реальным записям usage журналов (тарифы — из конфига).
                let report =
                    arch_harness::metrics::cost_report(&cfg.paths.sessions_dir, &cfg.models);
                outp!("{}", arch_harness::metrics::render_cost_report(&report));
                return Ok(());
            }
            let mut m =
                arch_harness::metrics::collect(&cfg.paths.sessions_dir, &cfg.paths.reports_dir)?;
            // Денежная стоимость — только по тарифам моделей из конфига
            // (None — тарифы не заданы, выдуманного курса нет).
            m.total_cost =
                arch_harness::metrics::cost_report(&cfg.paths.sessions_dir, &cfg.models).total_cost;
            // Architecture drift по реестру AGENTS.md (repos.txt), если он ведётся.
            let registry = arch_harness::config::Config::home_dir().join("repos.txt");
            if registry.is_file() {
                if let Ok(report) = arch_harness::agentsmd::lint_registry(&registry) {
                    m.agentsmd_total = report.len();
                    m.agentsmd_stale = report
                        .iter()
                        .filter(|(_, issues)| {
                            issues
                                .iter()
                                .any(|i| i.rule.contains("stale") || i.severity == "error")
                        })
                        .count();
                }
            }
            outln!("{}", m.to_markdown());
        }
        Some(Cmd::Resources { cmd }) => cmd_resources(&cfg, cmd)?,
        Some(Cmd::Experiment { cmd }) => cmd_experiment(&cfg, cmd)?,
        Some(Cmd::Ariadna { question }) => cmd_ariadna(&cfg, &question)?,
        Some(Cmd::Doctor) => {
            let checks = arch_harness::doctor::run_checks(&cfg);
            outp!("{}", arch_harness::doctor::render(&checks));
            if arch_harness::doctor::exit_code(&checks) != 0 {
                std::process::exit(1);
            }
        }
        Some(Cmd::Preflight {
            spec,
            example,
            json,
        }) => {
            if example {
                outp!("{}", arch_harness::preflight::example_spec());
                return Ok(());
            }
            let Some(spec_path) = spec else {
                anyhow::bail!("нужен SPEC.toml (или --example для образца)");
            };
            let spec = arch_harness::preflight::parse_spec(&spec_path)?;
            if json {
                outln!("{}", arch_harness::preflight::render_json(&spec));
            } else {
                let gates = arch_harness::preflight::run_preflight(&spec);
                outp!("{}", arch_harness::preflight::render(&gates));
                if arch_harness::preflight::exit_code(&gates) != 0 {
                    std::process::exit(1);
                }
            }
        }
        Some(Cmd::Export {
            format,
            session,
            out,
        }) => {
            let Some(fmt) = arch_harness::export::ExportFormat::parse(&format) else {
                return Err(anyhow::anyhow!(
                    "неизвестный формат «{format}» (ожидалось word|excel)"
                ));
            };
            let n = arch_harness::export::export_journal(&session, fmt, &out)?;
            outln!("экспортировано {n} строк → {}", out.display());
        }
        Some(Cmd::Delta { cmd }) => cmd_delta(cmd)?,
        Some(Cmd::Openspec { cmd }) => cmd_openspec(cmd)?,
        Some(Cmd::AgentsMd { cmd }) => cmd_agents_md(&cfg, cmd)?,
        Some(Cmd::Cron { cmd }) => cmd_cron(&cfg, cmd).await?,
        Some(Cmd::Eval { cmd }) => cmd_eval(&cfg, cmd).await?,
        Some(Cmd::Worktree { cmd }) => cmd_worktree(&cfg, cmd).await?,
        Some(Cmd::Fleet { cmd }) => cmd_fleet(&cfg, cmd).await?,
        Some(Cmd::Survey { repo, out }) => cmd_survey(&repo, out.as_deref())?,
        Some(Cmd::Archunit { cmd }) => cmd_archunit(cmd).await?,
        Some(Cmd::Weights { cmd }) => cmd_weights(&cfg, cmd)?,
        Some(Cmd::DataCard { cmd }) => cmd_data_card(&cfg, cmd)?,
        Some(Cmd::Trajectory { cmd }) => cmd_trajectory(&cfg, cmd)?,
    }
    Ok(())
}

/// `arch-ml archunit …`: `ArchUnit`-мост (ADR-039).
async fn cmd_archunit(cmd: ArchunitCmd) -> Result<()> {
    match cmd {
        ArchunitCmd::Gen {
            repo,
            constraints,
            model_dir,
            out_dir,
            base_package,
        } => {
            let c = constraints.unwrap_or_else(|| repo.join(".arch-handoff/CONSTRAINTS.yaml"));
            let rules = arch_harness::control::load_fitness_rules(&c)?;
            let refs: Vec<&arch_harness::control::FitnessRule> = rules.iter().collect();
            let mut spec =
                arch_harness::archunit::spec_from_constraints(&repo, &refs, model_dir.as_deref());
            if let Some(bp) = base_package {
                spec.base_package = Some(bp);
            }
            let out = out_dir.unwrap_or_else(|| repo.join("archunit-fitness"));
            std::fs::create_dir_all(&out)
                .with_context(|| format!("не создать каталог вывода {}", out.display()))?;
            let test_path = out.join("ArchFitnessTest.java");
            let json_path = out.join("archunit-rules.json");
            std::fs::write(&test_path, arch_harness::archunit::render_junit_test(&spec))
                .with_context(|| format!("не записать {}", test_path.display()))?;
            std::fs::write(&json_path, arch_harness::archunit::spec_to_json(&spec)?)
                .with_context(|| format!("не записать {}", json_path.display()))?;
            outln!(
                "спек: {} правил ArchUnit, {} не смаплено (unsupported)",
                spec.rules.len(),
                spec.unsupported.len()
            );
            for u in &spec.unsupported {
                outln!("  [warn] {}: {}", u.rule, u.reason);
            }
            outln!("JUnit-тест: {}", test_path.display());
            outln!("спек JSON:  {}", json_path.display());
        }
        ArchunitCmd::Check {
            repo,
            constraints,
            model_dir,
            classes,
            jar_dir,
            timeout_secs,
            json,
        } => {
            let c = constraints.unwrap_or_else(|| repo.join(".arch-handoff/CONSTRAINTS.yaml"));
            let rules = arch_harness::control::load_fitness_rules(&c)?;
            let refs: Vec<&arch_harness::control::FitnessRule> = rules.iter().collect();
            let spec =
                arch_harness::archunit::spec_from_constraints(&repo, &refs, model_dir.as_deref());
            for u in &spec.unsupported {
                eprintln!("[warn] unsupported: {}: {}", u.rule, u.reason);
            }
            let classes_dir = match classes {
                Some(dir) => dir,
                None => arch_harness::archunit::find_classes_dir(&repo).with_context(|| {
                    "archunit: скомпилированные классы не найдены (target/classes, \
                     build/classes/java/main, out/production, classes) — соберите проект \
                     (`mvn compile` / `javac -d classes ...`) или укажите --classes"
                })?,
            };
            let opts = arch_harness::archunit::GateOptions {
                classes_dir,
                jar_dir: arch_harness::archunit::resolve_jar_dir(jar_dir.as_deref()),
                runner_cache: arch_harness::archunit::default_runner_cache(),
                timeout: std::time::Duration::from_secs(
                    timeout_secs.unwrap_or(arch_harness::archunit::DEFAULT_GATE_TIMEOUT_SECS),
                ),
            };
            let outcome = arch_harness::archunit::run_gate(&spec, &opts)?;
            let severity_of = |rule_id: &str| {
                spec.rules
                    .iter()
                    .find(|r| r.id == rule_id)
                    .map_or("error", |r| r.severity.as_str())
            };
            let passed = outcome
                .violations
                .iter()
                .all(|v| severity_of(&v.rule_id) != "error");
            if json {
                let report = serde_json::json!({
                    "repo": repo,
                    "passed": passed,
                    "rules_executed": outcome.rules_executed,
                    "violations": outcome.violations.iter().map(|v| serde_json::json!({
                        "rule": v.rule_id,
                        "severity": severity_of(&v.rule_id),
                        "detail": v.detail,
                    })).collect::<Vec<_>>(),
                    "unsupported": spec.unsupported,
                });
                outln!("{report}");
            } else {
                outln!(
                    "ArchUnit-гейт: исполнено правил {}, нарушений {}",
                    outcome.rules_executed,
                    outcome.violations.len()
                );
                for v in &outcome.violations {
                    outln!(
                        "  [{}] {} — {}",
                        severity_of(&v.rule_id),
                        v.rule_id,
                        v.detail
                    );
                }
                outln!("Итог: {}", if passed { "PASS" } else { "FAIL" });
            }
            if !passed {
                std::process::exit(1);
            }
        }
        ArchunitCmd::Fetch { jar_dir } => {
            let dest = jar_dir.unwrap_or_else(|| {
                arch_harness::config::Config::home_dir()
                    .join("archunit")
                    .join("lib")
            });
            let fetched = arch_harness::archunit::fetch_jars(&dest).await?;
            outln!("jar'ы ArchUnit → {}", dest.display());
            for f in &fetched {
                outln!(
                    "  {} {} sha256:{}",
                    f.file,
                    if f.cached {
                        "(кэш)"
                    } else {
                        "(скачан)"
                    },
                    f.sha256
                );
            }
        }
    }
    Ok(())
}

/// `arch-ml survey <repo>`: обратное обследование legacy → docs/reverse/survey.md.
fn cmd_survey(repo: &Path, out: Option<&Path>) -> Result<()> {
    let outcome = arch_harness::survey::run(repo, out)?;
    outln!(
        "обследование `{}`: {} находок [confirmed], {} секций [gap]",
        outcome.report.repo_name,
        outcome.report.confirmed_count(),
        outcome.report.gap_count()
    );
    outln!("карта: {}", outcome.survey_path.display());
    if outcome.notes_created {
        outln!(
            "создана заготовка заметок [inferred]: {}",
            outcome.notes_path.display()
        );
    }
    Ok(())
}

/// `arch-ml fleet`: аудит флота worktree (дубли/дрейф) и гейт мерджа прогонов.
async fn cmd_fleet(cfg: &Arc<Config>, cmd: FleetCmd) -> Result<()> {
    match cmd {
        FleetCmd::Audit {
            paths,
            repo,
            include,
            format,
            stable,
            fail_on_dupes,
        } => {
            let mut roots = paths;
            if let Some(repo) = repo {
                roots.extend(arch_harness::fleet::worktrees_from_git(&repo)?);
            }
            let report = arch_harness::fleet::audit(&roots, &include)?;
            match format.as_str() {
                "json" => outln!("{}", serde_json::to_string_pretty(&report)?),
                "text" => outp!("{}", arch_harness::fleet::render_text_opts(&report, stable)),
                other => anyhow::bail!("неизвестный формат '{other}' (допустимы: text, json)"),
            }
            // Независимые триггеры гейта: дрейф копий и порог доли дублей.
            let dupes_failed = fail_on_dupes.is_some_and(|pct| report.dup_pct > pct);
            if let Some(pct) = fail_on_dupes {
                if dupes_failed {
                    outln!(
                        "Порог дублей превышен: {:.1}% > {pct:.1}% — exit 1",
                        report.dup_pct
                    );
                }
            }
            if report.has_drift || dupes_failed {
                std::process::exit(1);
            }
        }
        FleetCmd::Merge {
            run_id,
            owner_approve,
            approver,
            repo,
        } => {
            let repo = match repo {
                Some(r) => r,
                None => std::env::current_dir().context("cwd")?,
            };
            match arch_harness::worktree::gated_merge(
                cfg,
                &repo,
                &run_id,
                owner_approve,
                approver.as_deref(),
            )
            .await?
            {
                arch_harness::worktree::MergeGateOutcome::Refused(summary) => {
                    outln!("{summary}");
                    std::process::exit(1);
                }
                arch_harness::worktree::MergeGateOutcome::Merged(msg) => outln!("{msg}"),
            }
        }
        FleetCmd::Run {
            repo,
            items_file,
            plan,
            package,
            judge,
        } => {
            let state_dir = cfg.paths.state_dir.clone();
            let agents = arch_harness::fleet_run::harness_profiles(cfg);
            match (plan, items_file) {
                (Some(plan_path), _) => {
                    let plan = arch_harness::fleet_plan::parse_plan(&plan_path)?;
                    let outcome = arch_harness::fleet_exec::run_plan(
                        cfg,
                        &state_dir,
                        &repo,
                        plan,
                        &agents,
                        arch_harness::fleet_exec::ResumeMode::Fresh,
                        judge.as_deref(),
                    )
                    .await?;
                    outp!(
                        "{}",
                        arch_harness::fleet_exec::render_plan_outcome(&outcome)
                    );
                    if outcome.halted.is_some() || outcome.n_failed > 0 {
                        std::process::exit(1);
                    }
                }
                (None, Some(items_file)) => {
                    let text = std::fs::read_to_string(&items_file)
                        .with_context(|| format!("не читается {}", items_file.display()))?;
                    let value: serde_json::Value = serde_json::from_str(&text)?;
                    let items = arch_harness::fleet_run::parse_items(&value)?;
                    let outcome = arch_harness::fleet_run::run_fleet(
                        cfg, &state_dir, &repo, &package, &items, &agents, None,
                    )
                    .await?;
                    outp!("{}", arch_harness::fleet_run::render_outcome(&outcome));
                }
                (None, None) => anyhow::bail!(
                    "fleet run: укажите --plan <файл плана> (паттерны) или \
                     --items-file <json> (legacy-веер)"
                ),
            }
        }
        FleetCmd::Plan { cmd } => cmd_fleet_plan(cfg, cmd)?,
        FleetCmd::Resume {
            run_id,
            repo,
            force_rerun,
        } => {
            let repo = match repo {
                Some(r) => r,
                None => std::env::current_dir().context("cwd")?,
            };
            let agents = arch_harness::fleet_run::harness_profiles(cfg);
            let state_dir = cfg.paths.state_dir.clone();
            let outcome = arch_harness::fleet_exec::resume_plan(
                cfg,
                &state_dir,
                &repo,
                &run_id,
                force_rerun,
                &agents,
                None,
            )
            .await?;
            outp!(
                "{}",
                arch_harness::fleet_exec::render_plan_outcome(&outcome)
            );
            if outcome.halted.is_some() || outcome.n_failed > 0 {
                std::process::exit(1);
            }
        }
        FleetCmd::Status { run_id, json } => {
            let fleet_dir = cfg.paths.state_dir.join("fleet");
            let path = if run_id == "latest" {
                arch_harness::fleet_run::latest_log(&fleet_dir)?
            } else {
                fleet_dir.join(format!("{run_id}.jsonl"))
            };
            if json {
                let read = arch_harness::fleet_run::FleetLog::read_tolerant(&path)?;
                let nodes = arch_harness::fleet_exec::completed_nodes(&path)?;
                let summary = serde_json::json!({
                    "run_id": run_id,
                    "log_path": path,
                    "events": read.events.len(),
                    "skipped_lines": read.skipped,
                    "node_gates": nodes,
                });
                outln!("{}", serde_json::to_string_pretty(&summary)?);
            } else {
                outp!("{}", arch_harness::fleet_run::render_log(&path)?);
            }
        }
        FleetCmd::Control {
            run_id,
            agent,
            kill,
            pause,
            resume,
            reroute,
            priority,
        } => {
            let (cmd, payload, label) = if kill {
                (ControlCommand::Kill, None, "Kill")
            } else if pause {
                (ControlCommand::Pause, None, "Pause")
            } else if resume {
                (ControlCommand::Resume, None, "Resume")
            } else if let Some(target) = reroute {
                (ControlCommand::Reroute, Some(target), "Reroute")
            } else if let Some(n) = priority {
                (ControlCommand::Priority, Some(n.to_string()), "Priority")
            } else {
                anyhow::bail!(
                    "fleet control: укажите действие (--kill | --pause | --resume | \
                     --reroute <target> | --priority <n>)"
                );
            };
            let fleet_dir = cfg.paths.state_dir.join("fleet");
            let ch = arch_harness::fleet_run::ControlChannel::new(
                arch_harness::fleet_run::control_path(&fleet_dir, &run_id),
            );
            ch.send(&agent, cmd, payload.as_deref())?;
            outln!("{label} отправлен агенту '{agent}' в прогоне {run_id}");
        }
    }
    Ok(())
}

/// `arch-ml fleet plan`: предложение, проверка и просмотр планов флота (ADR-042).
fn cmd_fleet_plan(cfg: &Arc<Config>, cmd: FleetPlanCmd) -> Result<()> {
    use arch_harness::fleet_plan;
    match cmd {
        FleetPlanCmd::Propose {
            repo,
            pattern,
            out,
            from_adrs,
            adr_dir,
            status,
        } => {
            let repo = match repo {
                Some(r) => r,
                None => std::env::current_dir().context("cwd")?,
            };
            let forced = match pattern {
                Some(p) => Some(fleet_plan::PatternKind::parse(&p)?),
                None => None,
            };
            let target = out.unwrap_or_else(|| repo.join(&cfg.fleet.plan_dir));
            if from_adrs {
                let dir = match adr_dir {
                    Some(d) if d.is_absolute() => d,
                    Some(d) => repo.join(d),
                    None => repo.join(fleet_plan::ADR_DIR),
                };
                let filter = match status {
                    Some(s) => fleet_plan::AdrStatusFilter::parse(&s)?,
                    None => fleet_plan::AdrStatusFilter::Proposed,
                };
                let proposal = fleet_plan::propose_from_adrs(&dir, filter, forced)
                    .with_context(|| format!("план из ADR каталога '{}'", dir.display()))?;
                let path = fleet_plan::write_plan(&proposal.plan, &target)
                    .with_context(|| format!("запись плана в '{}'", target.display()))?;
                outln!("План записан: {}", path.display());
                outp!("{}", proposal.rationale);
                for w in &proposal.warnings {
                    outln!("  ⚠ {w}");
                }
                let issues = fleet_plan::validate_plan(&proposal.plan);
                if !issues.is_empty() {
                    outln!("\nПроверка черновика:");
                    for issue in &issues {
                        outln!("  {:?}: {}", issue.severity, issue.message);
                    }
                }
                // Гейт независимости — отказ, а не совет: пара без пути с общим
                // путём записи не может стартовать (ADR-045). План-черновик уже
                // записан — владелец правит рёбра, а не генерирует заново.
                if issues
                    .iter()
                    .any(|i| i.severity == fleet_plan::PlanSeverity::Error)
                {
                    std::process::exit(1);
                }
            } else {
                if adr_dir.is_some() || status.is_some() {
                    anyhow::bail!("--adr-dir/--status имеют смысл только с --from-adrs");
                }
                let (plan, rationale) = fleet_plan::propose(&repo, forced)?;
                let path = fleet_plan::write_plan(&plan, &target)?;
                outln!("План записан: {}", path.display());
                outp!("{rationale}");
                let issues = fleet_plan::validate_plan(&plan);
                if !issues.is_empty() {
                    outln!("\nПроверка черновика:");
                    for issue in &issues {
                        outln!("  {:?}: {}", issue.severity, issue.message);
                    }
                }
            }
        }
        FleetPlanCmd::Validate { plan, json } => {
            let parsed = fleet_plan::parse_plan(&plan)?;
            let issues = fleet_plan::validate_plan(&parsed);
            let has_error = issues
                .iter()
                .any(|i| i.severity == fleet_plan::PlanSeverity::Error);
            if json {
                outln!("{}", serde_json::to_string_pretty(&issues)?);
            } else if issues.is_empty() {
                outln!("План {} валиден: замечаний нет", parsed.id);
            } else {
                for issue in &issues {
                    outln!("  {:?}: {}", issue.severity, issue.message);
                }
                outln!(
                    "Итог: {}",
                    if has_error {
                        "FAIL (exit 1)"
                    } else {
                        "PASS с замечаниями"
                    }
                );
            }
            if has_error {
                std::process::exit(1);
            }
        }
        FleetPlanCmd::Show {
            plan,
            mermaid,
            json,
        } => {
            let parsed = fleet_plan::parse_plan(&plan)?;
            let compiled = fleet_plan::compile(parsed)?;
            if mermaid {
                outp!("{}", fleet_plan::render_mermaid(&compiled));
            } else if json {
                let waves: Vec<Vec<String>> = compiled
                    .levels
                    .iter()
                    .map(|level| {
                        level
                            .iter()
                            .filter_map(|i| compiled.nodes().get(*i).map(|n| n.id.clone()))
                            .collect()
                    })
                    .collect();
                let summary = serde_json::json!({
                    "id": compiled.plan.id,
                    "pattern": compiled.plan.pattern.as_str(),
                    "max_parallel": compiled.plan.policy.max_parallel,
                    "require_worktree": compiled.plan.policy.require_worktree,
                    "merge_gate": compiled.plan.policy.merge_gate,
                    "nodes": compiled.nodes().len(),
                    "waves": waves,
                    "warnings": compiled.warnings,
                });
                outln!("{}", serde_json::to_string_pretty(&summary)?);
            } else {
                outp!("{}", fleet_plan::render_plan(&compiled));
            }
        }
    }
    Ok(())
}

/// `arch-ml worktree`: изоляция агентной работы (создание, review, accept, drop).
async fn cmd_worktree(cfg: &Arc<Config>, cmd: WorktreeCmd) -> Result<()> {
    let cwd = std::env::current_dir().context("cwd")?;
    let repo_of = |repo: Option<PathBuf>| repo.unwrap_or_else(|| cwd.clone());
    match cmd {
        WorktreeCmd::New { name, repo, base } => {
            let path =
                arch_harness::worktree::create(cfg, &repo_of(repo), &name, base.as_deref()).await?;
            outln!("worktree создан: {}", path.display());
            outln!(
                "review: arch-ml worktree diff {name} · accept: arch-ml worktree accept {name} · drop: arch-ml worktree drop {name}"
            );
        }
        WorktreeCmd::List { repo } => {
            let infos = arch_harness::worktree::list(&repo_of(repo)).await?;
            outp!("{}", arch_harness::worktree::render_list(&infos));
        }
        WorktreeCmd::Diff { name, repo } => {
            outln!(
                "{}",
                arch_harness::worktree::diff(&repo_of(repo), &name).await?
            );
        }
        WorktreeCmd::Accept {
            name,
            repo,
            approver,
        } => {
            outln!(
                "{}",
                arch_harness::worktree::accept(cfg, &repo_of(repo), &name, approver.as_deref())
                    .await?
            );
        }
        WorktreeCmd::Drop { name, repo } => {
            outln!(
                "{}",
                arch_harness::worktree::drop(cfg, &repo_of(repo), &name).await?
            );
        }
    }
    Ok(())
}

/// `arch-ml resources`: инвентарь, живая детекция и выбор ресурса локальных GPU.
fn cmd_resources(cfg: &Config, cmd: ResourcesCmd) -> Result<()> {
    match cmd {
        ResourcesCmd::List => {
            outln!(
                "Локальные GPU ([[gpus.local]]) — объявлено {}:",
                cfg.gpus.local.len()
            );
            if cfg.gpus.local.is_empty() {
                outln!("  (пусто — добавьте [[gpus.local]] в config.toml)");
            }
            for g in &cfg.gpus.local {
                let spec = arch_harness::gpu::resolve_device(&g.device);
                let vram = g.vram_gb.or(spec.map(|d| d.vram_gb)).unwrap_or(0.0);
                let known = spec.map_or("неизвестно", |d| d.key);
                let ssh = g
                    .ssh
                    .as_deref()
                    .map_or(String::new(), |a| format!(", ssh {a}"));
                outln!(
                    "  {:<16} {:<12} {vram:>5.0} ГБ  (device = \"{}\"{ssh})",
                    g.name,
                    known,
                    g.device
                );
            }
            outln!();
            outln!(
                "Реестр устройств ({}):",
                arch_harness::gpu::known_devices().len()
            );
            for d in arch_harness::gpu::known_devices() {
                let kind = if d.unified { "unified" } else { "VRAM" };
                outln!(
                    "  {:<14} {:>5.0} ГБ ({kind})  [{}]",
                    d.key,
                    d.vram_gb,
                    d.source
                );
            }
        }
        ResourcesCmd::Check => {
            outln!("Объявлено локальных GPU: {}", cfg.gpus.local.len());
            // Локальная детекция (GPU на этой машине — без ssh-алиаса).
            let has_local = cfg.gpus.local.iter().any(|g| g.ssh.is_none());
            if has_local {
                match arch_harness::gpu::detect() {
                    Ok(gpus) => {
                        outln!("nvidia-smi (эта машина) увидел {} GPU:", gpus.len());
                        outp!("{}", arch_harness::gpu::render_detected(&gpus));
                    }
                    Err(e) => outln!("локальная детекция недоступна: {e:#}"),
                }
            }
            // Удалённые GPU по SSH-алиасу (read-only, не грузит GPU): unified-память
            // (GB10) читается из /proc/meminfo, дискретная — из nvidia-smi memory.*.
            for g in cfg.gpus.local.iter().filter(|g| g.ssh.is_some()) {
                let alias = g.ssh.as_deref().unwrap_or("?");
                let unified =
                    arch_harness::gpu::resolve_device(&g.device).is_some_and(|d| d.unified);
                outln!("{} (ssh {alias}):", g.name);
                let result = if unified {
                    arch_harness::gpu::detect_remote_unified(alias)
                        .map(|m| arch_harness::gpu::render_unified(&m))
                } else {
                    arch_harness::gpu::detect_remote(alias)
                        .map(|gpus| arch_harness::gpu::render_detected(&gpus))
                };
                match result {
                    Ok(text) => outp!("{text}"),
                    Err(e) => outln!("  недоступна: {e:#}"),
                }
            }
        }
        ResourcesCmd::Recommend {
            params_b,
            dtype,
            cloud_vram,
            live,
        } => {
            if params_b <= 0.0 {
                anyhow::bail!("params_b должен быть > 0");
            }
            let needed = arch_harness::gpu::required_vram_gb(params_b, &dtype);
            outln!("Куда гнать модель {params_b}B ({dtype}) — нужно ≈{needed:.1} ГБ:");
            for g in &cfg.gpus.local {
                let spec = arch_harness::gpu::resolve_device(&g.device);
                let total = g.vram_gb.or(spec.map(|d| d.vram_gb)).unwrap_or(0.0);
                let ssh = g
                    .ssh
                    .as_deref()
                    .map_or_else(|| "локально".to_string(), |a| format!("ssh {a}"));
                let (icon, note) = if live {
                    match arch_harness::gpu::live_free_gb(g) {
                        Some(f) if f >= needed => {
                            ("✓", format!("свободно {f:.0} ГБ — влезает сейчас"))
                        }
                        Some(f) => ("✗", format!("свободно лишь {f:.0} ГБ — не влезает сейчас")),
                        None => ("?", "недоступна — свободно неизвестно".to_string()),
                    }
                } else if total >= needed {
                    ("✓", "влезает ($0)".to_string())
                } else {
                    ("✗", "не влезает".to_string())
                };
                outln!(
                    "  {icon} local:{:<14} {total:>5.0} ГБ всего ({ssh}) — {note}",
                    g.name
                );
            }
            let cicon = if cloud_vram >= needed { "✓" } else { "✗" };
            outln!("  {cicon} cloud           {cloud_vram:>5.0} ГБ — платно, $/час");
        }
    }
    Ok(())
}

/// `arch-ml experiment`: provenance эксперимента (record/list/reproduce).
fn cmd_experiment(cfg: &Config, cmd: ExperimentCmd) -> Result<()> {
    let state_dir = &cfg.paths.state_dir;
    match cmd {
        ExperimentCmd::Record { spec, repo } => {
            let spec = arch_harness::preflight::parse_spec(&spec)?;
            if spec.name.trim().is_empty() {
                anyhow::bail!("в спеке нужно имя (name = \"...\")");
            }
            let repo = repo
                .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
            // SSH-алиас удалённого GPU: по device из спеки находим объявленный
            // локальный ресурс в [gpus.local] — его драйвер пишем как remote.
            let remote_ssh = spec.device.as_deref().and_then(|dev| {
                cfg.gpus
                    .local
                    .iter()
                    .find(|g| g.device.eq_ignore_ascii_case(dev))
                    .and_then(|g| g.ssh.clone())
            });
            let p = arch_harness::provenance::capture(&spec, &repo, remote_ssh.as_deref());
            let path = arch_harness::provenance::record(&p, state_dir)?;
            outln!("Provenance записан: {}", path.display());
            outp!("{}", arch_harness::provenance::render(&p));
        }
        ExperimentCmd::List => {
            let ids = arch_harness::provenance::list(state_dir)?;
            if ids.is_empty() {
                outln!("нет записей (запишите: arch-ml experiment record SPEC.toml)");
            } else {
                outln!("Эксперименты ({}):", ids.len());
                for id in ids {
                    outln!("  {id}");
                }
            }
        }
        ExperimentCmd::Reproduce { id, repo } => {
            let repo = repo
                .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
            outp!(
                "{}",
                arch_harness::provenance::reproduce(&id, state_dir, &repo)?
            );
        }
    }
    Ok(())
}

/// `arch-ml ariadna`: детерминированный роутинг вопроса к модели Ariadna.
fn cmd_ariadna(cfg: &Config, question: &str) -> Result<()> {
    let index = &cfg.concept.index;
    let db = if index.as_os_str().is_empty() {
        None
    } else {
        Some(arch_harness::concept::ConceptDb::open_cached(index)?)
    };
    let r = arch_harness::ariadna::route(question, db.as_deref());
    outln!("{} — {}", r.expert.as_str(), r.reason);
    Ok(())
}

/// `arch-ml init`: конфиг + ассеты в ~/.arch-ml.
fn cmd_init(cfg: &Config) -> Result<()> {
    let home = Config::home_dir();
    std::fs::create_dir_all(&home).context("создание домашнего каталога")?;
    let written = arch_harness::assets::write_defaults(&home)?;
    let cfg_path = cfg.save_default()?;
    outln!("Инициализация завершена:");
    outln!("  конфиг:  {}", cfg_path.display());
    outln!("  домашний каталог: {}", home.display());
    for f in &written {
        outln!("  ассет:   {}", f.display());
    }
    Ok(())
}

/// Опции headless-прогона `arch-ml run` (бюджеты).
struct RunOptions {
    /// Общий таймаут прогона, секунды.
    timeout_secs: Option<u64>,
    /// Переопределение `agent.max_tool_turns` на прогон.
    max_turns: Option<u64>,
    /// Спека goal-режима (`objective || criterion || check`); `None` —
    /// обычный одиночный прогон.
    goal: Option<String>,
}

/// `arch-ml run`: headless агент.
///
/// Строгий режим (`--quiet`, как `dsh --profile headless` у `DeepSeek`
/// Harness) = без стриминга: stdout несёт ТОЛЬКО финальный ответ ассистента
/// (пригоден для пайпов), события хода молчат; пустая задача отклоняется
/// до запуска; сбой — причина в stderr и ненулевой код выхода.
///
/// Бюджеты: `--timeout SECS` — общий потолок прогона (превышение — причина
/// в stderr и exit 1), `--max-turns N` — лимит итераций инструментов
/// (перекрывает `agent.max_tool_turns` на клоне конфига).
///
/// В стрим-режиме stdout несёт только текст ответа (дельты); прогресс
/// (вызовы инструментов, заметки) уходит в stderr — пайп остаётся чистым.
async fn cmd_run(
    cfg: &Arc<Config>,
    prompt: Option<String>,
    model: Option<String>,
    stream: bool,
    think: Option<String>,
    opts: RunOptions,
) -> Result<i32> {
    // Гол-режим: цель несёт задачу сама — промпт не обязателен.
    let goal_spec = match opts.goal.as_deref() {
        Some(raw) => {
            Some(arch_harness::goal::GoalSpec::parse_set(raw).context("--goal: разбор цели")?)
        }
        None => None,
    };
    let input = match prompt.as_deref() {
        Some("-") | None if !std::io::stdin().is_terminal() => {
            let mut buf = String::new();
            std::io::stdin()
                .read_to_string(&mut buf)
                .context("чтение stdin")?;
            buf
        }
        Some(p) => p.to_string(),
        None if goal_spec.is_some() => String::new(),
        None => anyhow::bail!("нет промпта: передайте аргумент или пайп в stdin"),
    };
    if input.trim().is_empty() && goal_spec.is_none() {
        anyhow::bail!("пустая задача: передайте непустой промпт аргументом или пайпом в stdin");
    }
    let thinking = match think.as_deref() {
        Some("on") => Some(true),
        Some("off") => Some(false),
        Some(other) => anyhow::bail!("--think: ожидается on|off, получено '{other}'"),
        None => None,
    };

    // Переопределение бюджета итераций — на клоне конфига, глобальный не трогаем.
    let cfg = match opts.max_turns {
        Some(n) => {
            let mut owned = (**cfg).clone();
            owned.agent.max_tool_turns =
                usize::try_from(n).context("--max-turns: значение не помещается в usize")?;
            Arc::new(owned)
        }
        None => cfg.clone(),
    };

    let registry = Arc::new(LlmRegistry::from_config(&cfg)?);
    let provider = match &model {
        Some(name) => registry.get(name)?,
        None => registry.default(),
    };
    let tools = arch_harness::tools::full_registry(&cfg);
    let cwd = std::env::current_dir().context("cwd")?;
    // Путь проекта строкой — до того, как `cwd` уедет в `ToolContext`
    // (владелец один): нужен как аргумент доменного хука `intent`.
    let repo = cwd.to_string_lossy().into_owned();
    let tool_ctx = ToolContext::new(cwd, cfg.clone())
        .with_llm(registry.clone())
        .with_provider(provider.clone())
        .with_subagents(arch_harness::subagent::SubagentRegistry::new());
    let system = default_system_prompt(&cfg);
    let mut session = AgentSession::new(cfg.clone(), provider, tools, tool_ctx, system);
    session.set_thinking(thinking);
    // Гол-режим: цель ставится до первого хода, а сам ход запускает
    // continuation — цель материализуется в контексте как `user`-сообщение
    // (спека `aiml/notes/goal-mode.md` §2). Дальше петля продолжает сама.
    let input = if let Some(spec) = goal_spec {
        // Роутинг по фактам проекта: текст намерения уходит доменным хуком
        // `intent` (плагин hypothesis-router) сразу после приёма цели, до
        // первого хода, — «after text received, before REQ/NFR». Строка
        // теплицы идёт в stderr (прогресс-канал): в stream/quiet-режиме
        // stdout пайпа несёт только ответ модели. Хук не установлен или
        // промолчал — строки нет, и выдумывать гипотезы нечем.
        let intent_hint = arch_harness::agent::slash::intent_line(
            &cfg.plugins.dirs,
            Path::new(&repo),
            &spec.objective,
        );
        session.goal_set(spec);
        if let Some(hint) = intent_hint {
            let _ = writeln!(std::io::stderr().lock(), "\x1b[2m» {hint}\x1b[0m");
        }
        session.goal_kickoff_message().unwrap_or(input)
    } else {
        input
    };

    let send = async {
        if stream {
            let (tx, mut rx) = tokio::sync::mpsc::channel(64);
            let printer = tokio::spawn(async move {
                use arch_harness::agent::AgentEvent;
                while let Some(ev) = rx.recv().await {
                    // StdoutLock/StderrLock не Send — лочим на каждое событие,
                    // не через await.
                    match ev {
                        AgentEvent::Delta(text) => {
                            let mut out = std::io::stdout().lock();
                            let _ = out.write_all(text.as_bytes());
                            let _ = out.flush();
                        }
                        // «Мысли» — приглушённо в stderr (прогресс-канал).
                        AgentEvent::ReasoningDelta(text) => {
                            let mut err = std::io::stderr().lock();
                            let _ = write!(err, "\x1b[2m{text}\x1b[0m");
                            let _ = err.flush();
                        }
                        // Прогресс — в stderr: stdout пайпа несёт только ответ.
                        AgentEvent::ToolStart { name, .. } => {
                            let _ =
                                writeln!(std::io::stderr().lock(), "\x1b[2m▶ tool: {name}\x1b[0m");
                        }
                        AgentEvent::ToolEnd {
                            name,
                            is_error,
                            summary,
                            ..
                        } => {
                            let mark = if is_error { "✗" } else { "✓" };
                            let _ = writeln!(
                                std::io::stderr().lock(),
                                "\x1b[2m{mark} {name}: {summary}\x1b[0m"
                            );
                        }
                        AgentEvent::Note(text) => {
                            let _ = writeln!(std::io::stderr().lock(), "\x1b[2m» {text}\x1b[0m");
                        }
                        AgentEvent::TurnDone => {
                            let _ = writeln!(std::io::stdout().lock());
                        }
                        // Телеметрия индикатора контекста — только для TUI.
                        AgentEvent::ContextUsage(_) => {}
                    }
                }
            });
            let r = session.send(&input, Some(tx)).await;
            let _ = printer.await;
            r.map_err(anyhow::Error::from)
        } else {
            session
                .send(&input, None)
                .await
                .map_err(anyhow::Error::from)
        }
    };
    let reply = match opts.timeout_secs {
        Some(secs) => match tokio::time::timeout(Duration::from_secs(secs), send).await {
            Ok(r) => r,
            Err(_) => Err(anyhow::anyhow!(
                "таймаут прогона ({secs}с): провайдер или инструмент не ответил вовремя"
            )),
        },
        None => send.await,
    };
    // Гол-режим: сбой хода — автопауза цели (спека §8), а не потеря прогона.
    // Наружу — код 6 «заморожен»; причина и состояние цели в журнале сессии,
    // продолжить — `/goal resume` или повторный запуск.
    let reply = match reply {
        Ok(reply) => reply,
        Err(e) if session.goal().is_some() => {
            let _ = writeln!(
                std::io::stderr().lock(),
                "\x1b[2m» цель приостановлена: {e}\x1b[0m"
            );
            return Ok(session.goal_exit_code().unwrap_or(6));
        }
        Err(e) => return Err(e),
    };
    if !stream {
        // Печать через writeln с игнорированием BrokenPipe: `arch-ml run -q … | head`
        // обрывает stdout — для пайпа это норма, а не повод для паники outln!.
        let mut out = std::io::stdout().lock();
        let _ = writeln!(out, "{reply}");
        let _ = out.flush();
    }
    // Цель без гол-режима не ставилась — код 0 (поведение прежних версий).
    Ok(session.goal_exit_code().unwrap_or(0))
}

/// Системный промпт по умолчанию: из библиотеки промптов или встроенный,
/// дополненный глобальной md-памятью (`paths.memory_file`, см. `memory`).
fn default_system_prompt(cfg: &Config) -> String {
    let dir = cfg.paths.prompts_dir();
    let base = match arch_harness::agent::prompts::load_library(&dir) {
        Ok(lib) => match lib.iter().find(|t| t.name == "architect") {
            Some(tpl) => tpl.body.clone(),
            None => fallback_system_prompt(),
        },
        Err(_) => fallback_system_prompt(),
    };
    // Ошибка чтения памяти не фатальна: сессия работает без неё.
    let memory = arch_harness::memory::load(&cfg.paths.memory_file)
        .ok()
        .flatten();
    arch_harness::memory::augment_system_prompt(&base, memory.as_deref(), &cfg.paths.memory_file)
}

/// Встроенный системный промпт (fallback, когда библиотека недоступна).
fn fallback_system_prompt() -> String {
    "Ты — solution-архитектор в контуре AI/ML-исследователя. Помогаешь проектировать \
     решения, ведёшь ADR и architecture-spine, оцениваешь архитектуру по рубрикам, \
     готовишь handoff-пакеты кодовым агентам. Отвечай по-русски, точно и по делу."
        .into()
}

/// `arch-ml prompts`.
fn cmd_prompts(cfg: &Config, name: Option<String>) -> Result<()> {
    let lib = arch_harness::agent::prompts::load_library(&cfg.paths.prompts_dir())?;
    match name {
        None => {
            outln!(
                "Библиотека промптов ({}):",
                cfg.paths.prompts_dir().display()
            );
            for tpl in &lib {
                outln!("  {:<24} {}", tpl.name, tpl.description);
            }
        }
        Some(n) => {
            let tpl = lib
                .iter()
                .find(|t| t.name == n)
                .with_context(|| format!("шаблон '{n}' не найден"))?;
            outln!("{}", tpl.body);
        }
    }
    Ok(())
}

/// `arch-ml memory [add <текст>]`: путь и содержимое глобальной md-памяти
/// либо дописка заметки в конец файла.
fn cmd_memory(cfg: &Config, cmd: Option<MemoryCmd>) -> Result<()> {
    let path = &cfg.paths.memory_file;
    match cmd {
        None => match arch_harness::memory::load(path)? {
            Some(content) => outln!("Память ({}):\n{content}", path.display()),
            None => outln!(
                "память пустая, файл: {} (дописать — arch-ml memory add <текст>)",
                path.display()
            ),
        },
        Some(MemoryCmd::Add { text }) => {
            arch_harness::memory::append(path, &text)?;
            outln!("заметка дописана в память: {}", path.display());
        }
    }
    Ok(())
}

/// `arch-ml govern`: формальная подотчётность (H1.1).
fn cmd_govern(cfg: &Config, cmd: Option<GovernCmd>) -> Result<()> {
    let state = &cfg.paths.state_dir;
    match cmd {
        None => {
            let recs = arch_harness::governance::govern_show(state)?;
            if recs.is_empty() {
                outln!("журнал решений пуст (записать — arch-ml govern record ...)");
            } else {
                outln!("Журнал решений ({}):", recs.len());
                for r in recs {
                    outln!(
                        "- [{}] {} ({}) — {}",
                        r.at,
                        r.approver,
                        r.spec_version,
                        r.decision
                    );
                }
            }
        }
        Some(GovernCmd::Record {
            approver,
            spec_version,
            artifact,
            decision,
        }) => {
            let path = arch_harness::governance::govern_record(
                state,
                &approver,
                &spec_version,
                artifact.as_deref(),
                &decision,
            )?;
            outln!("решение записано: {}", path.display());
        }
        Some(GovernCmd::Show) => {
            for r in arch_harness::governance::govern_show(state)? {
                outln!(
                    "[{}] {} ({}) — {} {}",
                    r.at,
                    r.approver,
                    r.spec_version,
                    r.decision,
                    r.artifact.as_deref().unwrap_or("")
                );
            }
        }
        Some(GovernCmd::Verify { artifact }) => {
            let ok = arch_harness::governance::govern_verify(state, artifact.as_deref())?;
            if ok {
                outln!("PASS: подотчётность зафиксирована (именной аппрувер + версия спека)");
            } else {
                eprintln!("FAIL: нет записи решения с именным аппрувером");
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml accept`: независимый eval-бар (H1.2).
fn cmd_accept(cfg: &Config, cmd: Option<AcceptCmd>) -> Result<()> {
    let state = &cfg.paths.state_dir;
    match cmd {
        None => {
            for r in arch_harness::governance::accept_show(state)? {
                outln!(
                    "[{}] spec {} — {} тестов",
                    r.at,
                    r.spec_version,
                    r.tests.len()
                );
                for t in &r.tests {
                    outln!("  - {t}");
                }
            }
        }
        Some(AcceptCmd::Set {
            spec_version,
            tests,
        }) => {
            let tests: Vec<String> = tests
                .split(',')
                .map(std::string::ToString::to_string)
                .collect();
            let path = arch_harness::governance::accept_set(state, &spec_version, tests)?;
            outln!("приёмочные тесты записаны: {}", path.display());
        }
        Some(AcceptCmd::Show) => {
            for r in arch_harness::governance::accept_show(state)? {
                outln!("[{}] spec {}", r.at, r.spec_version);
                for t in &r.tests {
                    outln!("  - {t}");
                }
            }
        }
        Some(AcceptCmd::Check { spec_version }) => {
            let ok = arch_harness::governance::accept_check(state, &spec_version)?;
            if ok {
                outln!("PASS: приёмочные тесты для spec {spec_version} зафиксированы");
            } else {
                eprintln!("FAIL: нет приёмочных тестов для spec {spec_version}");
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml evolve`: guarded harness evolution (H2.2, ADR-046 п.3).
fn cmd_evolve(cfg: &Config, cmd: Option<EvolveCmd>) -> Result<()> {
    let state = &cfg.paths.state_dir;
    match cmd {
        None | Some(EvolveCmd::List) => {
            for p in arch_harness::evolve::evolve_list(state)? {
                outln!(
                    "[{}] {} → {} (mode {}, block {}) — {}",
                    p.status,
                    p.id,
                    p.target,
                    p.mode,
                    p.block_id,
                    p.approver
                );
            }
        }
        Some(EvolveCmd::Propose {
            id,
            target,
            block_id,
            content,
            content_file,
            mode,
            base_sha256,
            approver,
        }) => {
            let repo = std::env::current_dir().context("evolve: нет рабочего каталога")?;
            let body = match (content, content_file) {
                (Some(_), Some(_)) => {
                    anyhow::bail!("evolve: укажите ровно одно из --content / --content-file")
                }
                (Some(text), None) => text,
                (None, Some(path)) => std::fs::read_to_string(&path).with_context(|| {
                    format!("evolve: не читается --content-file {}", path.display())
                })?,
                (None, None) => {
                    anyhow::bail!("evolve: нужен --content (block) или --content-file (file)")
                }
            };
            let req = arch_harness::evolve::ProposeRequest {
                id: &id,
                target: &target,
                mode: &mode,
                block_id: block_id.as_deref().unwrap_or(""),
                content: &body,
                approver: &approver,
                base_sha256: base_sha256.as_deref(),
            };
            let path = arch_harness::evolve::evolve_propose(state, &repo, &req)?;
            outln!("предложение записано: {}", path.display());
        }
        Some(EvolveCmd::Commit { id, judge }) => {
            let repo = std::env::current_dir().context("evolve: нет рабочего каталога")?;
            let proposal = arch_harness::evolve::evolve_find(state, &id)?;
            // Правка ядра (mode=file) требует baseline-судью; managed-блоки
            // доменного слоя гейт не проходят (поведение прежнее).
            let gate = if proposal.mode.trim() == arch_harness::evolve::MODE_FILE {
                let resolved = arch_harness::evolve::resolve_judge(
                    judge.as_deref(),
                    cfg.fleet.judge_binary.as_deref(),
                    &repo,
                )?;
                Some(arch_harness::evolve::GatePlan::baseline(resolved))
            } else {
                None
            };
            let p = arch_harness::evolve::evolve_commit(state, &id, &repo, gate.as_ref())?;
            // file-режим вносит контент уже на коммите (гейт судит внесённый
            // контент), поэтому `apply` для него — явный отказ, а не молчаливый
            // no-op: повторно применять нечего.
            if p.mode.trim() != arch_harness::evolve::MODE_FILE {
                arch_harness::evolve::evolve_apply(&repo, &p)?;
            }
            outln!(
                "предложение {} (mode={}) committed + применено к {}",
                p.id,
                p.mode,
                p.target
            );
        }
        Some(EvolveCmd::Reject { id }) => {
            arch_harness::evolve::evolve_reject(state, &id)?;
            outln!("предложение {id} отклонено");
        }
    }
    Ok(())
}

/// Прокси к Archify CLI (`node <archify.mjs> …`): печатает вывод, код
/// возврата CLI становится кодом возврата `arch-ml` (гейт для CI/скриптов).
async fn cmd_archify(cfg: &Config, cmd: ArchifyCmd) -> Result<()> {
    let cwd = std::env::current_dir().context("archify: не удалось определить рабочий каталог")?;
    let args: Vec<String> = match &cmd {
        ArchifyCmd::Doctor => vec!["doctor".into()],
        ArchifyCmd::Guide { query } => vec!["guide".into(), query.clone(), "--json".into()],
        ArchifyCmd::Validate {
            r#type,
            path,
            quality,
            json: _,
        } => vec![
            "validate".into(),
            r#type.clone(),
            path.to_string_lossy().into_owned(),
            "--quality".into(),
            quality.clone(),
            "--json".into(),
        ],
        ArchifyCmd::Deliver {
            r#type,
            path,
            output,
            quality,
            json: _,
        } => vec![
            "deliver".into(),
            r#type.clone(),
            path.to_string_lossy().into_owned(),
            output.to_string_lossy().into_owned(),
            "--quality".into(),
            quality.clone(),
            "--json".into(),
        ],
        ArchifyCmd::Compare {
            base,
            head,
            output,
            quality,
            json: _,
        } => vec![
            "compare".into(),
            "architecture".into(),
            base.to_string_lossy().into_owned(),
            head.to_string_lossy().into_owned(),
            output.to_string_lossy().into_owned(),
            "--quality".into(),
            quality.clone(),
            "--json".into(),
        ],
    };
    let run = arch_harness::archify::run(cfg, &cwd, &args, cfg.archify.timeout_secs).await?;
    // Для validate/deliver/compare отдаём компактную сводку receipt;
    // doctor/guide — сырой вывод CLI (текст/JSON рекомендации).
    // Флаг --json: сырой JSON-receipt Archify CLI (SDK-контракт v1).
    let json_mode = matches!(
        cmd,
        ArchifyCmd::Validate { json: true, .. }
            | ArchifyCmd::Deliver { json: true, .. }
            | ArchifyCmd::Compare { json: true, .. }
    );
    let summarize = !matches!(cmd, ArchifyCmd::Doctor | ArchifyCmd::Guide { .. });
    if json_mode {
        outp!("{}", run.stdout);
    } else if summarize {
        outp!(
            "{}",
            arch_harness::archify::summarize_receipt(&args[0], &run.stdout)
        );
    } else {
        outp!("{}", run.stdout);
    }
    if !run.stderr.trim().is_empty() {
        eprint!("{}", run.stderr);
    }
    if run.timed_out {
        anyhow::bail!(
            "archify {}: таймаут {} сек",
            args[0],
            cfg.archify.timeout_secs
        );
    }
    if !run.ok() {
        anyhow::bail!(
            "archify {}: провал (код выхода {})",
            args[0],
            run.status.map_or("?".to_string(), |c| c.to_string())
        );
    }
    Ok(())
}

async fn cmd_rubric(cfg: &Arc<Config>, cmd: RubricCmd) -> Result<()> {
    match cmd {
        RubricCmd::List => {
            let list = arch_harness::rubric::list(&cfg.paths.rubrics_dir())?;
            for r in &list {
                outln!(
                    "  {:<32} {} ({} критериев)",
                    r.name,
                    r.description,
                    r.criteria_count
                );
            }
        }
        RubricCmd::Run {
            rubric,
            target,
            model,
            dynamic_subject,
        } => {
            let registry = Arc::new(LlmRegistry::from_config(cfg)?);
            let judge = match &model {
                Some(name) => registry.get(name)?,
                None => registry.default(),
            };
            let text = std::fs::read_to_string(&target)
                .with_context(|| format!("чтение {}", target.display()))?;
            let rub = if let Some(subject) = dynamic_subject {
                let anchor_path = resolve_asset(&cfg.paths.rubrics_dir(), &rubric, "yaml");
                let anchor = arch_harness::rubric::load(&anchor_path).ok();
                arch_harness::rubric::generate_dynamic(&subject, anchor.as_ref(), judge.as_ref())
                    .await?
            } else {
                let path = resolve_asset(&cfg.paths.rubrics_dir(), &rubric, "yaml");
                arch_harness::rubric::load(&path)?
            };
            let report = arch_harness::rubric::evaluate(&rub, &text, judge.as_ref()).await?;
            outln!("{}", report.to_markdown());
            let out = cfg
                .paths
                .reports_dir
                .join(format!("rubric-{}-{}.md", rub.name, timestamp()));
            if let Some(parent) = out.parent() {
                std::fs::create_dir_all(parent).ok();
            }
            std::fs::write(&out, report.to_markdown())?;
            eprintln!("Отчёт: {}", out.display());
        }
    }
    Ok(())
}

async fn cmd_bench(cfg: &Arc<Config>, cmd: BenchCmd) -> Result<()> {
    match cmd {
        BenchCmd::List => {
            for b in arch_harness::bench::list(&cfg.paths.benchmarks_dir())? {
                outln!("  {:<32} {} [{}]", b.name, b.description, b.tags.join(", "));
            }
        }
        BenchCmd::Run {
            name,
            model,
            golden,
            record,
        } => {
            if record.is_some() && !golden {
                anyhow::bail!("`bench run --record` применим только с --golden");
            }
            let registry = LlmRegistry::from_config(cfg)?;
            let provider = match &model {
                Some(m) => registry.get(m)?,
                None => registry.default(),
            };
            if golden {
                if name.is_some() {
                    anyhow::bail!("`bench run --golden` не совместим с именем бенчмарка");
                }
                // Регрессионный гейт качества судьи (ADR-004): согласие с
                // эталоном ниже порога — exit 1, как у `control check`.
                let report = arch_harness::bench::run_golden(
                    provider.as_ref(),
                    &cfg.paths.rubrics_dir(),
                    &cfg.paths.benchmarks_dir().join("golden"),
                    &cfg.judge,
                )
                .await?;
                outln!(
                    "Golden-прогон судьи '{}' (сэмплов на критерий: {}):",
                    report.judge_model,
                    cfg.judge.samples
                );
                for case in &report.cases {
                    outln!(
                        "  {:<32} MAE {:.2} ({} критериев)",
                        case.doc,
                        case.mae,
                        case.compared
                    );
                }
                // Механическая диагностика: MAE по критериям и length bias.
                outp!("{}", report.diagnostics_text());
                let passed = report.mae <= cfg.judge.golden_max_mae;
                outln!(
                    "Итог MAE: {:.2} по {} парам (порог {:.2}) — {}",
                    report.mae,
                    report.compared,
                    cfg.judge.golden_max_mae,
                    if passed { "PASS" } else { "FAIL" }
                );
                // Evidence-журнал (M-2): запись не зависит от исхода гейта —
                // история хранит и регрессии.
                if let Some(path) = &record {
                    let entry = arch_harness::bench::GoldenRecord::from_report(
                        &report,
                        chrono::Local::now().format("%Y-%m-%d").to_string(),
                    );
                    arch_harness::bench::record_golden(path, &entry)?;
                    outln!("Записано в журнал: {}", path.display());
                }
                if !passed {
                    std::process::exit(1);
                }
                return Ok(());
            }
            let Some(name) = name else {
                anyhow::bail!("укажите имя бенчмарка или флаг --golden");
            };
            let path = resolve_asset(&cfg.paths.benchmarks_dir(), &name, "yaml");
            let bench = arch_harness::bench::load(&path)?;
            let report = arch_harness::bench::run(
                &bench,
                provider.as_ref(),
                &cfg.paths.rubrics_dir(),
                &cfg.paths.reports_dir,
                &cfg.judge,
            )
            .await?;
            outln!(
                "Бенчмарк '{}': {:.2} (порог {:.2}) — {}",
                report.bench_name,
                report.rubric_report.weighted_total,
                bench.pass_threshold,
                if report.passed { "PASS" } else { "FAIL" }
            );
        }
        BenchCmd::GoldenHistory { path } => {
            let (records, broken) = arch_harness::bench::load_golden_history(&path)?;
            if broken > 0 {
                eprintln!("пропущено битых строк: {broken}");
            }
            if records.is_empty() {
                outln!("Журнал {} пуст.", path.display());
            } else {
                outp!("{}", arch_harness::bench::golden_history_markdown(&records));
            }
        }
        BenchCmd::HumanAgreement { golden_dir, humans } => {
            let report = arch_harness::bench::human_agreement(&golden_dir, &humans)?;
            for doc in &report.skipped {
                eprintln!("пропущен {doc}: нет человеческих анкет");
            }
            outp!("{}", report.to_markdown());
        }
    }
    Ok(())
}

async fn cmd_web(cfg: &Config, cmd: WebCmd) -> Result<()> {
    match cmd {
        WebCmd::Search { query, arch } => {
            let results = if arch {
                arch_harness::web::search_arch_sites(&query, &[], &cfg.web).await?
            } else {
                arch_harness::web::search(&query, &cfg.web).await?
            };
            for r in &results {
                outln!("• {}\n  {}\n  {}\n", r.title, r.url, r.snippet);
            }
            if results.is_empty() {
                outln!("Ничего не найдено.");
            }
        }
        WebCmd::Fetch { url } => {
            let text = arch_harness::web::fetch(&url, &cfg.web).await?;
            outln!("{text}");
        }
        WebCmd::Sites => {
            outln!("Кураторские сайты архитектора:");
            for s in arch_harness::web::curated_sites(&cfg.web) {
                outln!("  {:<16} {:<40} {}", s.name, s.base_url, s.description);
            }
        }
    }
    Ok(())
}

async fn cmd_mcp(cfg: &Arc<Config>, cmd: McpCmd) -> Result<()> {
    // Серверный режим (P1-2, ADR-008) обслуживает клиентов и не подключается
    // к серверам: mcp.json для него не требуется, уходим до его загрузки.
    if matches!(cmd, McpCmd::Serve) {
        return arch_harness::mcp_server::serve(Arc::clone(cfg))
            .await
            .context("MCP-сервер (stdio)");
    }
    let mut servers = arch_harness::mcp::load_servers(&cfg.mcp.servers_file)
        .with_context(|| format!("чтение {}", cfg.mcp.servers_file.display()))?;
    // Плагины тоже несут MCP-серверы (стандарт: plugin.json mcpServers / .mcp.json).
    if cfg.plugins.include_mcp {
        let plugins = arch_harness::plugin::discover(&cfg.plugins.dirs);
        servers.extend(arch_harness::plugin::mcp_servers(&plugins));
    }
    let manager =
        Arc::new(arch_harness::mcp::McpManager::connect(&servers, cfg.mcp.timeout_secs).await?);
    match cmd {
        McpCmd::List => {
            outln!("Серверы: {}", manager.server_names().join(", "));
            for spec in manager.tools().await {
                outln!("  {:<40} {}", spec.name, spec.description);
            }
        }
        McpCmd::Call { name, args } => {
            let args: serde_json::Value =
                serde_json::from_str(&args).context("невалидный JSON аргументов")?;
            let out = manager.call(&name, args).await?;
            outln!("{}", out.content);
        }
        // Недостижимо: Serve обработан выше возвратом до подключения к серверам.
        McpCmd::Serve => {}
    }
    manager.shutdown().await;
    Ok(())
}

/// `arch-ml adr`: реестр ADR по набору проектов (ADR-036).
fn cmd_adr(cmd: AdrCmd) -> Result<()> {
    match cmd {
        AdrCmd::Registry { root, json, strict } => {
            let report = arch_harness::adr_registry::build_registry(&root)?;
            if json {
                outln!(
                    "{}",
                    serde_json::to_string(&report).expect("RegistryReport сериализуется")
                );
            } else {
                outln!("{}", arch_harness::adr_registry::render_markdown(&report));
            }
            let code = arch_harness::adr_registry::exit_code(&report, strict);
            if code != 0 {
                std::process::exit(code);
            }
        }
    }
    Ok(())
}

/// Публикация артефактов в корпоративные системы (файловые адаптеры, ADR-033).
fn cmd_publish(cmd: PublishCmd) -> Result<()> {
    match cmd {
        PublishCmd::Confluence { file } => {
            let md = std::fs::read_to_string(&file)
                .map_err(|e| arch_harness::error::HarnessError::io(&file, e))?;
            outln!("{}", arch_harness::publish::markdown_to_confluence(&md));
        }
        PublishCmd::Jira { result, project } => {
            outp!(
                "{}",
                arch_harness::publish::handoff_json_to_jira_csv(&result, project.as_deref())?
            );
        }
    }
    Ok(())
}

/// Дефолтный ruleset control-команд: явный `--constraints`, иначе рабочий
/// `<repo>/CONSTRAINTS.yaml`, иначе — как fallback — пакетный
/// `<repo>/.arch-handoff/CONSTRAINTS.yaml`. Возвращает путь и метку источника
/// (`None` — путь задан явно, объявлять нечего).
fn control_ruleset(
    repo: &Path,
    explicit: Option<PathBuf>,
) -> Result<(PathBuf, Option<arch_harness::control::ResolvedRuleset>)> {
    if let Some(path) = explicit {
        return Ok((path, None));
    }
    let resolved = arch_harness::control::resolve_ruleset_required(repo)?;
    Ok((resolved.path.clone(), Some(resolved)))
}

/// Предупреждение в stderr, если найден только пакетный файл (PASS по
/// заготовке — не полный контроль). stdout не трогает: JSON-отчёты и
/// markdown-отчёты остаются парсимыми.
fn warn_ruleset(repo: &Path, resolved: Option<&arch_harness::control::ResolvedRuleset>) {
    if let Some(warning) = resolved.and_then(|r| r.warning(repo)) {
        eprintln!("[warning] {warning}");
    }
}

/// Объявляет источник правил (stdout) + предупреждение о заготовке (stderr).
/// Только для `check` — гейта, чей контракт прямо требует печатать, какой
/// ruleset проверялся.
fn announce_ruleset(repo: &Path, resolved: Option<&arch_harness::control::ResolvedRuleset>) {
    warn_ruleset(repo, resolved);
    let Some(resolved) = resolved else {
        return;
    };
    outln!(
        "Ruleset: {} ({})",
        resolved.path.display(),
        resolved.kind.label()
    );
}

fn cmd_control(cfg: &arch_harness::config::Config, cmd: ControlCmd) -> Result<()> {
    match cmd {
        ControlCmd::Check {
            repo,
            constraints,
            json,
        } => {
            let (c, resolved) = control_ruleset(&repo, constraints)?;
            let report = arch_harness::control::check(&repo, &c)?;
            if json {
                // JSON-контракт: источник и предупреждение не подмешиваются
                // в stdout (парсер отчёта), только в stderr.
                warn_ruleset(&repo, resolved.as_ref());
                // SDK-контракт v1: машиночитаемый отчёт, exit code как в текстовом режиме.
                outln!(
                    "{}",
                    serde_json::to_string(&report).expect("FitnessReport сериализуется")
                );
            } else {
                announce_ruleset(&repo, resolved.as_ref());
                outln!("{}", report.summary);
                // Наследование корп-спайна (extends): метки источников видны
                // в выводе (docs/corp-spine.md).
                if !report.inherited.is_empty() {
                    let sources = report
                        .inherited
                        .iter()
                        .map(|s| format!("{} ({})", s.source, s.rules))
                        .collect::<Vec<_>>()
                        .join(", ");
                    outln!("Источники правил: {sources}");
                }
                for o in &report.overrides {
                    outln!(
                        "  [override:{}] {} (adr {}, until {}) — {}",
                        o.status,
                        o.rule,
                        o.adr,
                        o.until,
                        o.note
                    );
                }
                for i in &report.issues {
                    outln!(
                        "  [{}] {}:{} {} — {}",
                        i.severity,
                        i.file.display(),
                        i.line,
                        i.rule,
                        i.message
                    );
                }
                // Топ-5 самых медленных правил — только если есть правила > 1s.
                let mut slow: Vec<&arch_harness::control::RuleDuration> =
                    report.durations.iter().filter(|d| d.ms > 1000).collect();
                slow.sort_by(|a, b| b.ms.cmp(&a.ms).then(a.rule.cmp(&b.rule)));
                if !slow.is_empty() {
                    outln!("Самые медленные правила:");
                    for d in slow.iter().take(5) {
                        outln!("  {:.1}s {}", d.ms as f64 / 1000.0, d.rule);
                    }
                }
                outln!("Итог: {}", if report.passed { "PASS" } else { "FAIL" });
            }
            if !report.passed {
                std::process::exit(1);
            }
        }
        ControlCmd::Spine { file } => {
            let issues = arch_harness::control::lint_spine(&file)?;
            if issues.is_empty() {
                outln!("spine: нарушений нет");
            }
            for i in &issues {
                outln!(
                    "[{}] {}:{} {} — {}",
                    i.severity,
                    i.file.display(),
                    i.line,
                    i.rule,
                    i.message
                );
            }
            // Гейт в CI: error-находки ломают сборку (warn — только отчёт).
            let errors = issues.iter().filter(|i| i.severity == "error").count();
            if !issues.is_empty() {
                outln!("Итог: {} находок (error: {errors})", issues.len());
            }
            if errors > 0 {
                std::process::exit(1);
            }
        }
        ControlCmd::Sensors { dir } => {
            for r in arch_harness::control::sensors_check(&dir)? {
                outln!(
                    "  [{}] {} {} — {}",
                    if r.passed { "PASS" } else { "FAIL" },
                    r.sensor,
                    r.file.display(),
                    r.details
                );
            }
        }
        ControlCmd::Score { trigger, from_diff } => {
            // Пороги маршрутов — из конфига ([significance], ADR-034);
            // невалидные границы — понятная ошибка при чтении.
            let (fast_max, standard_max) = cfg
                .significance
                .limits()
                .map_err(|e| anyhow::anyhow!("{e}"))?;
            let mut answers = std::collections::BTreeMap::new();
            for t in &trigger {
                let (k, v) = t
                    .split_once('=')
                    .with_context(|| format!("триггер '{t}' не вида имя=true"))?;
                answers.insert(k.to_string(), v == "true");
            }
            if let Some(git_ref) = from_diff {
                // S-1 anti-bypass: «HEAD» (дефолт флага) — рабочее дерево
                // против HEAD; иное значение — GIT_REF...HEAD.
                let git_ref = (git_ref != "HEAD").then_some(git_ref);
                let diff = arch_harness::control::detect_diff_triggers(
                    std::path::Path::new("."),
                    git_ref.as_deref(),
                )?;
                let scored = arch_harness::control::score_with_sources(
                    &answers,
                    &diff,
                    fast_max,
                    standard_max,
                );
                let fired = scored
                    .significance
                    .fired
                    .iter()
                    .map(|f| {
                        scored
                            .sources
                            .get(f)
                            .map_or_else(|| f.clone(), |s| format!("{f} ({})", s.label()))
                    })
                    .collect::<Vec<_>>()
                    .join(", ");
                outln!(
                    "Score: {} ({fired} триггеров) → маршрут {:?}",
                    scored.significance.score,
                    scored.significance.route
                );
                for e in &diff.evidence {
                    outln!("  diff: {e}");
                }
                if !scored.undeclared.is_empty() {
                    outln!(
                        "ВНИМАНИЕ — расхождение: заявлено флагами vs видно по диффу: {}",
                        scored.undeclared.join(", ")
                    );
                }
            } else {
                let s = arch_harness::control::significance_score_with_limits(
                    &answers,
                    fast_max,
                    standard_max,
                );
                outln!(
                    "Score: {} ({} триггеров) → маршрут {:?}",
                    s.score,
                    s.fired.join(", "),
                    s.route
                );
            }
        }
        ControlCmd::RulesReport { repo, constraints } => {
            let (c, resolved) = control_ruleset(&repo, constraints)?;
            warn_ruleset(&repo, resolved.as_ref());
            outp!("{}", arch_harness::control::rules_report(&repo, &c)?);
        }
        ControlCmd::Report {
            repo,
            constraints,
            level,
            json,
        } => {
            let (c, resolved) = control_ruleset(&repo, constraints)?;
            let report = arch_harness::control::control_report(&repo, &c, &level)?;
            if json {
                warn_ruleset(&repo, resolved.as_ref());
                // SDK-контракт v1: машиночитаемый отчёт; report — отчётность,
                // exit code гейт не дублирует.
                outln!(
                    "{}",
                    serde_json::to_string(&report).expect("ControlReport сериализуется")
                );
            } else {
                warn_ruleset(&repo, resolved.as_ref());
                outp!("{}", arch_harness::control::render_control_report(&report));
            }
        }
        ControlCmd::Adr { title, dir } => {
            let dir = dir.unwrap_or_else(|| PathBuf::from("docs/adr"));
            let path = arch_harness::control::adr_new(&dir, &title)?;
            outln!("ADR создан: {}", path.display());
        }
        ControlCmd::Gate {
            gate,
            packet,
            rehearse,
            require_rehearsal,
        } => {
            use arch_harness::rehearsal as rh;
            if !gate.eq_ignore_ascii_case("a4") {
                anyhow::bail!("гейт '{gate}' не реализован механически (пока только A4)");
            }
            let requirement: rh::RehearsalRequirement = require_rehearsal
                .parse()
                .map_err(|e: String| anyhow::anyhow!("--require-rehearsal: {e}"))?;
            let (repo, packet_dir) = rh::locate_packet(&packet)?;
            let route = rh::packet_route(&packet_dir)?;
            let report = if rehearse {
                let report = rh::rehearse(&repo, &packet_dir)?;
                outln!("Репетиция отката (baseline {}):", report.baseline_commit);
                for line in &report.log {
                    outln!("  {line}");
                }
                outln!(
                    "  evidence: {}",
                    packet_dir.join(rh::REHEARSAL_FILE).display()
                );
                Some(report)
            } else {
                rh::load_report(&packet_dir)?
            };
            // Свежесть evidence сверяем с текущим планом (если он читается).
            let plan_baseline = rh::load_plan(&packet_dir)
                .ok()
                .map(|p| p.baseline_commit.trim().to_string());
            let verdict = rh::gate_a4(
                route,
                requirement,
                plan_baseline.as_deref(),
                report.as_ref(),
            );
            outln!("{}", verdict.summary);
            outln!("Итог: {}", if verdict.passed { "PASS" } else { "FAIL" });
            if !verdict.passed {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml model`: типизированная модель архитектуры (ADR-003).
fn cmd_model(cmd: ModelCmd) -> Result<()> {
    match cmd {
        ModelCmd::Validate { dir } => {
            let model = arch_harness::model::load_model(&dir)
                .with_context(|| format!("загрузка модели {}", dir.display()))?;
            let report = arch_harness::model::validate(&model);
            for i in &report.issues {
                outln!(
                    "[{}] {}: {} — {}",
                    i.severity,
                    i.file.display(),
                    i.rule,
                    i.message
                );
            }
            outln!("{}", report.summary());
            outln!(
                "Итог: {}",
                if report.has_errors() { "FAIL" } else { "PASS" }
            );
            if report.has_errors() {
                std::process::exit(1);
            }
        }
        ModelCmd::Show { id, dir } => {
            let model = arch_harness::model::load_model(&dir)
                .with_context(|| format!("загрузка модели {}", dir.display()))?;
            let entity = model
                .get(&id)
                .with_context(|| format!("сущность '{id}' не найдена в {}", dir.display()))?;
            outp!("{}", arch_harness::model::card(&model, entity));
        }
        ModelCmd::Graph { dir, format } => {
            let model = arch_harness::model::load_model(&dir)
                .with_context(|| format!("загрузка модели {}", dir.display()))?;
            match format.as_str() {
                "text" => outp!("{}", arch_harness::model::graph_text(&model)),
                "mermaid" => outp!("{}", arch_harness::model::graph_mermaid(&model)),
                other => anyhow::bail!("неизвестный формат '{other}' (допустимы: text, mermaid)"),
            }
        }
        ModelCmd::Project { dir } => {
            let report = arch_harness::model::project_adr(&dir)
                .with_context(|| format!("проекция модели {}", dir.display()))?;
            for f in &report.written {
                outln!("записан: {}", f.display());
            }
            for f in &report.removed {
                outln!("удалён (нет сущности): {}", f.display());
            }
            outln!(
                "Проекция {}: {} ADR-файлов, удалено устаревших: {}",
                report.out_dir.display(),
                report.written.len(),
                report.removed.len()
            );
        }
        ModelCmd::Export { dir, format } => {
            let fmt = arch_harness::model::ExportFormat::from_name(&format).with_context(|| {
                format!(
                    "неизвестный формат '{format}' (допустимы: {})",
                    arch_harness::model::ExportFormat::names()
                )
            })?;
            let model = arch_harness::model::load_model(&dir)
                .with_context(|| format!("загрузка модели {}", dir.display()))?;
            let text = arch_harness::model::export_model(&model, fmt)
                .with_context(|| format!("экспорт модели {}", dir.display()))?;
            outp!("{text}");
        }
        ModelCmd::Import { file, format, dir } => {
            if !format.trim().eq_ignore_ascii_case("structurizr") {
                anyhow::bail!(
                    "импорт поддерживает только --format structurizr (получено: '{format}')"
                );
            }
            let report = arch_harness::model::import_structurizr(&file, &dir)
                .with_context(|| format!("импорт {} в {}", file.display(), dir.display()))?;
            for f in &report.written {
                outln!("записан: {}", f.display());
            }
            for w in &report.warnings {
                outln!("предупреждение: {w}");
            }
            outln!(
                "Импорт {}: {} сущностей, предупреждений: {}",
                report.dir.display(),
                report.written.len(),
                report.warnings.len()
            );
        }
        ModelCmd::Landscape { root, mermaid } => {
            let report = arch_harness::landscape::build_landscape(&root)?;
            outln!("{}", arch_harness::landscape::render_markdown(&report));
            if mermaid {
                outln!("\n```mermaid");
                outln!("{}", arch_harness::landscape::render_mermaid(&report));
                outln!("```");
            }
        }
    }
    Ok(())
}

/// `arch-ml trace`: трассируемость модели как fitness-функция (ADR-006).
fn cmd_trace(cmd: TraceCmd) -> Result<()> {
    match cmd {
        TraceCmd::Check { dir } => {
            let report = arch_harness::trace::trace_check(&dir)
                .with_context(|| format!("трассировка кейса {}", dir.display()))?;
            outp!("{}", arch_harness::trace::render_markdown(&report));
            if report.has_errors() {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml nfr`: количественные NFR поверх модели (ADR-007); error — exit code 1.
fn cmd_nfr(cmd: NfrCmd) -> Result<()> {
    match cmd {
        NfrCmd::Budget { dir } => {
            let report = arch_harness::nfr::budget_check(&dir)
                .with_context(|| format!("latency-бюджет кейса {}", dir.display()))?;
            outp!("{}", report.render());
            if report.has_errors() {
                std::process::exit(1);
            }
        }
        NfrCmd::Availability { dir } => {
            let report = arch_harness::nfr::availability_check(&dir)
                .with_context(|| format!("расчёт доступности кейса {}", dir.display()))?;
            outp!("{}", report.render());
            if report.has_errors() {
                std::process::exit(1);
            }
        }
        NfrCmd::Capacity { dir } => {
            let report = arch_harness::nfr::capacity_check(&dir)
                .with_context(|| format!("расчёт ёмкости кейса {}", dir.display()))?;
            outp!("{}", report.render());
            if report.has_errors() {
                std::process::exit(1);
            }
        }
        NfrCmd::Cost { dir } => {
            let report = arch_harness::nfr::cost_check(&dir)
                .with_context(|| format!("расчёт стоимости кейса {}", dir.display()))?;
            outp!("{}", report.render());
            if report.has_errors() {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml skills`: библиотека скиллов.
fn cmd_skills(cfg: &Config, cmd: SkillsCmd) -> Result<()> {
    let plugins = arch_harness::plugin::discover(&cfg.plugins.dirs);
    match cmd {
        SkillsCmd::List => {
            let total: usize = plugins.iter().map(|p| p.skills.len()).sum();
            outln!("Скиллов: {total} в {} плагинах", plugins.len());
            for p in &plugins {
                for s in &p.skills {
                    outln!(
                        "  {:<28} {:<14} {}",
                        s.name,
                        p.manifest.name,
                        first_line(&s.description, 80)
                    );
                }
            }
        }
        SkillsCmd::Search { query, limit } => {
            let hits = arch_harness::plugin::search(&plugins, &query, limit);
            if hits.is_empty() {
                outln!(
                    "Ничего не найдено (скиллов в индексе: {}).",
                    plugins.iter().map(|p| p.skills.len()).sum::<usize>()
                );
            }
            for h in &hits {
                outln!(
                    "── {} [{}] (score {:.1})",
                    h.meta.name,
                    h.meta.plugin,
                    h.score
                );
                outln!("   {}", first_line(&h.meta.description, 100));
                if !h.snippet.is_empty() {
                    outln!("{}", h.snippet);
                }
            }
        }
        SkillsCmd::Show { name } => {
            let meta = arch_harness::plugin::skill_by_name(&plugins, &name)
                .with_context(|| format!("скилл '{name}' не найден (см. `arch-ml skills list`)"))?;
            outln!("{}", arch_harness::plugin::load_skill(meta)?);
        }
    }
    Ok(())
}

/// `arch-ml weights`: реестр артефактов ML-контура (`artifacts.yaml`).
fn cmd_weights(_cfg: &Config, cmd: WeightsCmd) -> Result<()> {
    match cmd {
        WeightsCmd::List { manifest } => {
            let path =
                manifest.unwrap_or_else(|| PathBuf::from(arch_harness::weights::DEFAULT_MANIFEST));
            let m = arch_harness::weights::load_manifest(&path)?;
            outln!("Артефактов: {} ({})", m.artifacts.len(), path.display());
            for a in &m.artifacts {
                outln!(
                    "  {:<24} {:<10} {}",
                    a.id,
                    artifact_kind_str(a.kind),
                    a.path.display()
                );
            }
        }
        WeightsCmd::Verify { manifest, json } => {
            let path =
                manifest.unwrap_or_else(|| PathBuf::from(arch_harness::weights::DEFAULT_MANIFEST));
            let m = arch_harness::weights::load_manifest(&path)?;
            let root = std::env::current_dir().context("текущий каталог")?;
            let report = arch_harness::weights::verify(&m, &root);
            if json {
                let (ok, warn, fail) = report.counts();
                let v = serde_json::json!({
                    "ok": ok,
                    "warn": warn,
                    "fail": fail,
                    "checks": report.checks.iter().map(|c| serde_json::json!({
                        "id": c.id,
                        "status": check_status_str(c.status),
                        "detail": c.detail,
                    })).collect::<Vec<_>>(),
                });
                outln!("{}", serde_json::to_string_pretty(&v)?);
            } else {
                outp!("{}", arch_harness::weights::render_report(&report));
            }
            if report.has_failures() {
                return Err(anyhow::anyhow!("проверка артефактов: есть провалы"));
            }
        }
    }
    Ok(())
}

/// Строковая метка роли артефакта.
fn artifact_kind_str(kind: Option<arch_harness::weights::ArtifactKind>) -> &'static str {
    match kind {
        Some(arch_harness::weights::ArtifactKind::Weights) => "weights",
        Some(arch_harness::weights::ArtifactKind::Dataset) => "dataset",
        Some(arch_harness::weights::ArtifactKind::Tokenizer) => "tokenizer",
        Some(arch_harness::weights::ArtifactKind::Other) => "other",
        None => "-",
    }
}

/// Строковая метка статуса проверки артефакта.
fn check_status_str(status: arch_harness::weights::CheckStatus) -> &'static str {
    match status {
        arch_harness::weights::CheckStatus::Ok => "ok",
        arch_harness::weights::CheckStatus::Warn => "warn",
        arch_harness::weights::CheckStatus::Fail => "fail",
    }
}

/// `arch-ml data-card`: датасет-карточки.
///
/// Семантика `check` зафиксирована ядром `dataset_card`:
/// - пустое обязательное поле — декларация-пробел, НЕ провал (карточек на
///   диске может не быть, ложно-красный недопустим);
/// - противоречия (`sha256`/`records` объявлены, а файла датасета нет)
///   проверяются только при явном `--dataset`: без пути «файла нет» не
///   доказать, поэтому без флага проблема не выдумывается.
///
/// `--strict` сохраняет смысл: и пробелы, и противоречия дают ненулевой код
/// возврата с разбивкой в тексте ошибки.
fn cmd_data_card(_cfg: &Config, cmd: DataCardCmd) -> Result<()> {
    match cmd {
        DataCardCmd::Check {
            cards,
            dataset,
            strict,
        } => {
            let root = cards.unwrap_or_else(|| PathBuf::from("."));
            let files = collect_card_files(&root)?;
            if files.is_empty() {
                outln!("Карточек не найдено: {}", root.display());
            }
            let mut gaps = 0usize;
            let mut problems = 0usize;
            for p in &files {
                let card = arch_harness::dataset_card::load_card(p)?;
                let mut report = arch_harness::dataset_card::validate(&card, dataset.as_deref());
                // В отчёте CLI остаётся путь карточки (семантика ядра:
                // `report.path` — путь известного вызывающему файла).
                report.path.clone_from(p);
                gaps += report.missing.len();
                problems += report.problems.len();
                outp!("{}", arch_harness::dataset_card::render_report(&report));
            }
            outln!(
                "Карточек: {} (пробелов: {gaps}, проблем: {problems})",
                files.len()
            );
            if strict && (gaps > 0 || problems > 0) {
                return Err(anyhow::anyhow!(
                    "data-card --strict: пробелов {gaps}, проблем {problems}"
                ));
            }
        }
    }
    Ok(())
}

/// Собрать YAML-файлы карточек из каталога (рекурсивно) или один файл.
fn collect_card_files(root: &Path) -> Result<Vec<PathBuf>> {
    if root.is_file() {
        return Ok(vec![root.to_path_buf()]);
    }
    let mut out = Vec::new();
    for entry in walkdir::WalkDir::new(root) {
        let entry = entry.with_context(|| format!("обход {}", root.display()))?;
        if !entry.file_type().is_file() {
            continue;
        }
        let is_yaml = entry
            .path()
            .extension()
            .is_some_and(|e| e.eq_ignore_ascii_case("yaml") || e.eq_ignore_ascii_case("yml"));
        if is_yaml {
            out.push(entry.path().to_path_buf());
        }
    }
    Ok(out)
}

/// `arch-ml trajectory`: метрики eval-траекторий.
fn cmd_trajectory(_cfg: &Config, cmd: TrajectoryCmd) -> Result<()> {
    match cmd {
        TrajectoryCmd::Metrics {
            input,
            format,
            k,
            json,
        } => {
            let fmt = if format == "auto" {
                detect_input_format(&input)?
            } else {
                parse_format_name(&format).ok_or_else(|| {
                    anyhow::anyhow!(
                        "неизвестный формат «{format}» (ожидалось auto|episode-jsonl|session-journal|selfplay)"
                    )
                })?
            };
            let episodes = arch_harness::trajectory::parse_episodes(&input, fmt)?;
            let m = arch_harness::trajectory::compute(&episodes, k);
            if json {
                let v = serde_json::json!({
                    "episodes": m.episodes,
                    "tasks": m.tasks,
                    "labeled": m.labeled,
                    "successes": m.successes,
                    "success_rate": m.success_rate,
                    "ci95": m.ci95,
                    "pass_at_k": m.pass_at_k,
                    "k": m.k,
                    "mean_steps": m.mean_steps,
                    "mean_tokens": m.mean_tokens,
                    "invalid_tool_calls": m.invalid_tool_calls,
                });
                outln!("{}", serde_json::to_string_pretty(&v)?);
            } else {
                outp!("{}", arch_harness::trajectory::render_metrics(&m));
            }
        }
    }
    Ok(())
}

/// Определить формат траекторий по первой значимой строке файла.
fn detect_input_format(path: &Path) -> Result<arch_harness::trajectory::InputFormat> {
    let text =
        std::fs::read_to_string(path).with_context(|| format!("чтение {}", path.display()))?;
    for line in text.lines() {
        if line.trim().is_empty() || line.trim_start().starts_with('#') {
            continue;
        }
        return arch_harness::trajectory::detect_format(line)
            .ok_or_else(|| anyhow::anyhow!("не удалось определить формат {}", path.display()));
    }
    Err(anyhow::anyhow!("файл пуст: {}", path.display()))
}

/// Разобрать имя формата траекторий (кроме `auto`).
fn parse_format_name(name: &str) -> Option<arch_harness::trajectory::InputFormat> {
    match name {
        "episode-jsonl" => Some(arch_harness::trajectory::InputFormat::EpisodeJsonl),
        "session-journal" => Some(arch_harness::trajectory::InputFormat::SessionJournal),
        "selfplay" => Some(arch_harness::trajectory::InputFormat::Selfplay),
        _ => None,
    }
}

/// `arch-ml plugins`: пакеты скиллов + MCP.
fn cmd_plugins(cfg: &Config, cmd: PluginsCmd) -> Result<()> {
    let plugins = arch_harness::plugin::discover(&cfg.plugins.dirs);
    match cmd {
        PluginsCmd::List => {
            outln!("Плагины ({}):", plugins.len());
            for p in &plugins {
                let mcp_count = if cfg.plugins.include_mcp {
                    arch_harness::plugin::mcp_servers(std::slice::from_ref(p)).len()
                } else {
                    0
                };
                outln!(
                    "  {:<24} v{:<8} скиллов: {:<3} mcp: {:<2} {}",
                    p.manifest.name,
                    p.manifest.version,
                    p.skills.len(),
                    mcp_count,
                    first_line(&p.manifest.description, 60)
                );
            }
        }
        PluginsCmd::Show { name } => {
            let p = plugins
                .iter()
                .find(|p| p.manifest.name == name)
                .with_context(|| format!("плагин '{name}' не найден"))?;
            outln!(
                "{} v{} — {}",
                p.manifest.name,
                p.manifest.version,
                p.manifest.description
            );
            outln!("Каталог: {}", p.dir.display());
            if !p.manifest.keywords.is_empty() {
                outln!("Ключевые слова: {}", p.manifest.keywords.join(", "));
            }
            outln!("Скиллы ({}):", p.skills.len());
            for s in &p.skills {
                outln!("  {:<28} {}", s.name, first_line(&s.description, 70));
            }
            let servers = arch_harness::plugin::mcp_servers(std::slice::from_ref(p));
            if !servers.is_empty() {
                outln!("MCP-серверы ({}):", servers.len());
                for s in &servers {
                    outln!("  {:<24} {} {}", s.name, s.command, s.args.join(" "));
                }
            }
        }
    }
    Ok(())
}

/// Первая строка текста, усечённая до `max` символов.
fn first_line(text: &str, max: usize) -> String {
    let line = text.lines().next().unwrap_or("").trim();
    let cut: String = line.chars().take(max).collect();
    if line.chars().count() > max {
        format!("{cut}…")
    } else {
        cut
    }
}

/// `arch-ml policy`: уровень автономии и классификация команды.
fn cmd_policy(cfg: &Config, check: Option<String>) -> Result<()> {
    let policy = arch_harness::policy::Policy::parse(&cfg.policy.autonomy)?;
    match check {
        None => {
            outln!(
                "Уровень автономии: R{} (из config [policy] autonomy)",
                policy.level
            );
            outln!(
                "  R0 — только чтения авто; R2 — + изменения (дефолт); R4 — деструктив с подтверждением; R5 — полная (красный флаг аудита)"
            );
        }
        Some(cmd) => {
            use arch_harness::policy::{PolicyDecision, classify_bash};
            let class = classify_bash(&cmd);
            let decision = policy.check("bash", &serde_json::json!({"command": cmd}));
            let verdict = match &decision {
                PolicyDecision::Allow => "ALLOW",
                PolicyDecision::RequireConfirm(_) => "REQUIRE-CONFIRM",
                PolicyDecision::Deny(_) => "DENY",
            };
            outln!(
                "команда: {cmd}\nкласс риска: {class:?}\nрешение (R{}): {verdict}",
                policy.level
            );
            match &decision {
                PolicyDecision::RequireConfirm(m) | PolicyDecision::Deny(m) => {
                    outln!("причина: {m}");
                }
                PolicyDecision::Allow => {}
            }
        }
    }
    Ok(())
}

/// `arch-ml evidence`: Evidence Bundle.
fn cmd_evidence(cmd: EvidenceCmd) -> Result<()> {
    match cmd {
        EvidenceCmd::Pack { dir, route } => {
            let route = match route.to_lowercase().as_str() {
                "fast" => arch_harness::control::Route::Fast,
                "critical" => arch_harness::control::Route::Critical,
                _ => arch_harness::control::Route::Standard,
            };
            let (bundle, verdict) = arch_harness::evidence::pack(&dir, route)?;
            outln!("{}", verdict.summary);
            for item in &bundle.items {
                outln!("  + {:<20} {} ({} б)", item.key, item.path, item.size);
            }
            for miss in &verdict.missing {
                outln!("  ✗ ОТСУТСТВУЕТ: {miss}");
            }
            outln!("Манифест: {}", dir.join("EVIDENCE.yaml").display());
            if !verdict.passed {
                std::process::exit(1);
            }
        }
        EvidenceCmd::Verify { dir } => {
            let v = arch_harness::evidence::verify(&dir)?;
            outln!("{}", v.summary);
            for m in &v.missing {
                outln!("  ✗ ОТСУТСТВУЕТ: {m}");
            }
            for t in &v.tampered {
                outln!("  ✗ ИЗМЕНЁН: {t}");
            }
            outln!(
                "Итог: {}",
                if v.passed {
                    "PASS — выпуск разрешён"
                } else {
                    "FAIL — выпуск заблокирован"
                }
            );
            if !v.passed {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml delta`: дельта-спецификации.
fn cmd_delta(cmd: DeltaCmd) -> Result<()> {
    let cwd = || std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    match cmd {
        DeltaCmd::New { name, repo } => {
            let path = arch_harness::delta::new(&repo.unwrap_or_else(cwd), &name)?;
            outln!("Дельта создана: {}", path.display());
        }
        DeltaCmd::List { repo } => {
            let list = arch_harness::delta::list(&repo.unwrap_or_else(cwd));
            if list.is_empty() {
                outln!("Дельт нет (changes/ пуст или отсутствует).");
            }
            for d in &list {
                outln!("  {:<30} {:?}", d.name, d.status);
            }
        }
        DeltaCmd::Validate { name, repo } => {
            let issues = arch_harness::delta::validate(&repo.unwrap_or_else(cwd), &name)?;
            if issues.is_empty() {
                outln!("дельта '{name}': нарушений нет");
            }
            let mut failed = false;
            for i in &issues {
                outln!(
                    "[{}] {}:{} {} — {}",
                    i.severity,
                    i.file.display(),
                    i.line,
                    i.rule,
                    i.message
                );
                failed |= i.severity == "error";
            }
            if failed {
                std::process::exit(1);
            }
        }
        DeltaCmd::Archive { name, repo } => {
            let path = arch_harness::delta::archive(&repo.unwrap_or_else(cwd), &name)?;
            outln!("Дельта заархивирована: {}", path.display());
        }
        DeltaCmd::Guard {
            repo,
            base,
            protect,
        } => {
            let report =
                arch_harness::delta::guard(&repo.unwrap_or_else(cwd), base.as_deref(), &protect)?;
            outp!("{}", arch_harness::delta::render_guard(&report));
            if !report.passed {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml openspec`: адаптер `OpenSpec` — требования → покрытие fitness-правилами.
fn cmd_openspec(cmd: OpenspecCmd) -> Result<()> {
    match cmd {
        OpenspecCmd::Scan { root, json } => {
            let requirements = arch_harness::openspec::scan_requirements(&root)?;
            if json {
                let report = arch_harness::openspec::ScanReport {
                    total: requirements.len(),
                    root,
                    requirements,
                };
                // SDK-контракт v1: машиночитаемый отчёт в stdout.
                outln!(
                    "{}",
                    serde_json::to_string(&report).expect("ScanReport сериализуется")
                );
            } else {
                outp!(
                    "{}",
                    arch_harness::openspec::render_scan(&root, &requirements)
                );
            }
        }
        OpenspecCmd::Coverage {
            root,
            constraints,
            json,
            strict,
        } => {
            let report = arch_harness::openspec::coverage(&root, constraints.as_deref())?;
            if json {
                outln!(
                    "{}",
                    serde_json::to_string(&report).expect("CoverageReport сериализуется")
                );
            } else {
                outp!("{}", report.to_markdown());
            }
            // Строгий режим: требования «без решения» ломают гейт (exit 1).
            if strict && report.unresolved > 0 {
                std::process::exit(1);
            }
        }
        OpenspecCmd::Init { root, out, force } => {
            let out_dir = out.unwrap_or_else(|| root.clone());
            let outcome = arch_harness::openspec::init(&root, &out_dir, force)?;
            outln!("Записано: {}", outcome.constraints_path.display());
            outln!("Записано: {}", outcome.spine_path.display());
            outln!(
                "Правил-заглушек: {}, кандидатов в спайн: {}, истории (archive): {}",
                outcome.rules,
                outcome.candidates,
                outcome.history
            );
            outp!("{}", outcome.coverage.to_markdown());
        }
        OpenspecCmd::Gate {
            archive,
            root,
            change_id,
            constraints,
        } => {
            if !archive {
                anyhow::bail!(
                    "реализован только гейт --archive (roadmap: --change, --expiry — docs/openspec.md)"
                );
            }
            let report =
                arch_harness::openspec::gate_archive(&root, &change_id, constraints.as_deref())?;
            outp!("{}", report.to_markdown());
            if !report.passed {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml agents-md`: AGENTS.md как канал архитектурного контроля.
fn cmd_agents_md(cfg: &Config, cmd: AgentsMdCmd) -> Result<()> {
    match cmd {
        AgentsMdCmd::Refresh { repo } => {
            let report = arch_harness::agentsmd::generate(&repo)?;
            outln!(
                "AGENTS.md: {} ({}) — инвариантов: {}, fitness: {}",
                report.path.display(),
                report.action,
                report.invariants,
                if report.has_constraints {
                    "да"
                } else {
                    "нет"
                }
            );
        }
        AgentsMdCmd::Lint { repo } => {
            let issues = arch_harness::agentsmd::lint(&repo)?;
            if issues.is_empty() {
                outln!("AGENTS.md свежий, нарушений нет");
            }
            let mut failed = false;
            for i in &issues {
                outln!(
                    "[{}] {}:{} {} — {}",
                    i.severity,
                    i.file.display(),
                    i.line,
                    i.rule,
                    i.message
                );
                failed |= i.severity == "error";
            }
            if failed {
                std::process::exit(1);
            }
        }
        AgentsMdCmd::LintAll { registry } => {
            let registry = registry.unwrap_or_else(|| Config::home_dir().join("repos.txt"));
            let results = arch_harness::agentsmd::lint_registry(&registry)?;
            let mut failed = false;
            for (repo, issues) in &results {
                let errors = issues.iter().filter(|i| i.severity == "error").count();
                let status = if issues.is_empty() {
                    "OK".to_string()
                } else {
                    format!("{} проблем ({} error)", issues.len(), errors)
                };
                outln!("{:<50} {}", repo.display(), status);
                failed |= errors > 0;
            }
            let _ = cfg;
            if failed {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

async fn cmd_cron(cfg: &Arc<Config>, cmd: CronCmd) -> Result<()> {
    let tab = arch_harness::cron::load(&cfg.cron.file)?;
    match cmd {
        CronCmd::List => {
            for j in &tab.jobs {
                outln!(
                    "  {:<24} {:<16} {}",
                    j.name,
                    j.schedule,
                    j.task_md.display()
                );
            }
        }
        CronCmd::Run { name } => {
            let job = tab
                .jobs
                .iter()
                .find(|j| j.name == name)
                .with_context(|| format!("задача '{name}' не найдена"))?;
            let registry = LlmRegistry::from_config(cfg)?;
            let provider = match &job.model {
                Some(m) => registry.get(m)?,
                None => registry.default(),
            };
            let tools = arch_harness::tools::full_registry(cfg);
            let out_dir = job
                .out
                .clone()
                .unwrap_or_else(|| cfg.paths.reports_dir.join("cron"));
            let path =
                arch_harness::cron::run_job(job, provider.as_ref(), &tools, &out_dir).await?;
            outln!("Отчёт: {}", path.display());
        }
        CronCmd::Tick => {
            // «Дюжные» задачи между прошлым тиком и сейчас; метка — в state-файле.
            let state_file = Config::home_dir().join("cron-last-tick");
            let now = chrono::Local::now();
            let last = std::fs::read_to_string(&state_file)
                .ok()
                .and_then(|s| {
                    chrono::DateTime::parse_from_rfc3339(s.trim())
                        .ok()
                        .map(|dt| dt.with_timezone(&chrono::Local))
                })
                .unwrap_or_else(|| now - chrono::Duration::hours(24));
            let registry = LlmRegistry::from_config(cfg)?;
            let provider = registry.default();
            let tools = arch_harness::tools::full_registry(cfg);
            let reports_dir = cfg
                .cron
                .out_dir
                .clone()
                .unwrap_or_else(|| cfg.paths.reports_dir.join("cron"));
            let reports = arch_harness::cron::run_due(
                &tab,
                last,
                now,
                provider.as_ref(),
                &tools,
                &reports_dir,
            )
            .await?;
            std::fs::write(&state_file, now.to_rfc3339()).context("запись метки тика")?;
            if reports.is_empty() {
                println!("Дюжных задач нет.");
            }
            for path in &reports {
                println!("Отчёт: {}", path.display());
            }
        }
    }
    Ok(())
}

/// `arch eval`: регрессионные eval-сьюты конфигурации харнесса (docs/evals.md).
///
/// Встроенный сьют (без `--suite`) герметичен: ассеты и конфиг разворачиваются
/// во временный каталог, живой `~/.arch-ml` не трогается — прогон зелёный
/// и в CI без `arch init`. Пользовательский `--suite` бежит против живой
/// установки. Гейт: pass-rate ниже `--gate` (дефолт 100%) — exit code 1.
async fn cmd_eval(cfg: &Arc<Config>, cmd: EvalCmd) -> Result<()> {
    match cmd {
        EvalCmd::Run {
            suite,
            gate,
            judge,
            model,
        } => {
            let gate_pct = gate.unwrap_or(100.0);
            if !(0.0..=100.0).contains(&gate_pct) {
                anyhow::bail!("--gate: ожидается процент 0..=100, получено {gate_pct}");
            }
            // tempdir держим живым до конца прогона (встроенный сьют).
            let mut _tmp = None;
            let (suite_dir, ctx) = if let Some(dir) = &suite {
                (dir.clone(), arch_harness::eval::SuiteContext::for_live()?)
            } else {
                let tmp = tempfile::tempdir().context("временный каталог встроенного сьюта")?;
                let home = arch_harness::eval::prepare_builtin_home(tmp.path())?;
                let ctx = arch_harness::eval::SuiteContext::for_builtin(tmp.path(), &home.config)?;
                _tmp = Some(tmp);
                (home.suite_dir, ctx)
            };
            // Слой судьи: реестр моделей строится только при --judge —
            // офлайн-прогон не требует ни ключей, ни сети.
            let provider = if judge {
                let registry = LlmRegistry::from_config(cfg)?;
                Some(match &model {
                    Some(name) => registry.get(name)?,
                    None => registry.default(),
                })
            } else {
                None
            };
            let rubrics_dir = cfg.paths.rubrics_dir();
            let judge_ctx = provider.as_ref().map(|p| arch_harness::eval::JudgeCtx {
                provider: p.as_ref(),
                cfg: &cfg.judge,
                rubrics_dir: &rubrics_dir,
            });
            let report =
                arch_harness::eval::run_suite(&suite_dir, &ctx, judge_ctx.as_ref(), gate_pct)
                    .await?;
            print!("{}", arch_harness::eval::render_text(&report));
            let out = arch_harness::eval::write_report(&report, &cfg.paths.evals_dir())?;
            eprintln!("Отчёт: {}", out.display());
            if !report.gate_passed {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// Читает файл или stdin (`-`).
fn read_file_or_stdin(file: &str) -> Result<String> {
    if file == "-" {
        let mut buf = String::new();
        std::io::stdin()
            .read_to_string(&mut buf)
            .context("чтение stdin")?;
        Ok(buf)
    } else {
        std::fs::read_to_string(file).with_context(|| format!("чтение {file}"))
    }
}

/// Резолвит имя ассета: точный путь, либо `<dir>/<name>`, либо `<dir>/<name>.<ext>`.
fn resolve_asset(dir: &std::path::Path, name: &str, ext: &str) -> PathBuf {
    let as_path = PathBuf::from(name);
    if as_path.is_file() {
        return as_path;
    }
    let in_dir = dir.join(name);
    if in_dir.is_file() {
        return in_dir;
    }
    dir.join(format!("{name}.{ext}"))
}

/// Есть ли бинарь в PATH.
fn which(binary: &str) -> String {
    std::process::Command::new("which")
        .arg(binary)
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map_or_else(
            || "MISSING".into(),
            |o| String::from_utf8_lossy(&o.stdout).trim().to_string(),
        )
}

/// Метка времени для имён отчётов.
fn timestamp() -> String {
    chrono::Local::now().format("%Y%m%d-%H%M%S").to_string()
}
