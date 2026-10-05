//! Архитектурный контроль: fitness-гейт `control` (`CONSTRAINTS.yaml`,
//! линтер спайна, сенсоры спецификаций, significance score с anti-bypass
//! floor ADR-034, корп-отчётность, гейт A4) и `ArchUnit`-мост `archunit`
//! (ADR-039) — JVM-гейты из того же файла ограничений.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::Subcommand;

/// Подкоманды `arch-ml archunit` (ADR-039).
#[derive(Subcommand)]
pub(crate) enum ArchunitCmd {
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

#[derive(Subcommand)]
pub(crate) enum ControlCmd {
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

/// `arch-ml archunit …`: `ArchUnit`-мост (ADR-039).
pub(crate) async fn cmd_archunit(cmd: ArchunitCmd) -> Result<()> {
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

/// Дефолтный ruleset control-команд: явный `--constraints`, иначе рабочий
/// `<repo>/CONSTRAINTS.yaml`, иначе — как fallback — пакетный
/// `<repo>/.arch-handoff/CONSTRAINTS.yaml`. Возвращает путь и метку источника
/// (`None` — путь задан явно, объявлять нечего).
fn control_ruleset(
    repo: &Path,
    explicit: Option<PathBuf>,
) -> Result<(PathBuf, Option<arch_harness::control::ResolvedRuleset>)> {
    // Несуществующий репозиторий — отдельная понятная ошибка (контракт SDK),
    // а не «ruleset не найден» с перечислением путей.
    if !repo.is_dir() {
        return Err(arch_harness::error::HarnessError::Control(format!(
            "репозиторий недоступен: {}",
            repo.display()
        ))
        .into());
    }
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

pub(crate) fn cmd_control(cfg: &arch_harness::config::Config, cmd: ControlCmd) -> Result<()> {
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
