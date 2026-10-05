//! Подотчётность и управляемая эволюция: формальный журнал решений с именным
//! аппрувером (`govern`, H1.1), независимый eval-бар — человеко-заданные
//! приёмочные тесты (`accept`, H1.2), guarded harness evolution (`evolve`,
//! H2.2, ADR-046 п.3: propose/commit доменного слоя и ядра с baseline-судьёй).

use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Subcommand;

use arch_harness::config::Config;

/// Подкоманды `arch-ml govern` (H1.1).
#[derive(Subcommand)]
pub(crate) enum GovernCmd {
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
pub(crate) enum AcceptCmd {
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
pub(crate) enum EvolveCmd {
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

/// `arch-ml govern`: формальная подотчётность (H1.1).
pub(crate) fn cmd_govern(cfg: &Config, cmd: Option<GovernCmd>) -> Result<()> {
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
pub(crate) fn cmd_accept(cfg: &Config, cmd: Option<AcceptCmd>) -> Result<()> {
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
pub(crate) fn cmd_evolve(cfg: &Config, cmd: Option<EvolveCmd>) -> Result<()> {
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
