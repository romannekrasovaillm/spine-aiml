//! # arch-harness — доменный харнесс solution-архитектора (бинарь `arch-ml`, Spine AI/ML Edition)
//!
//! Тонкий агентный харнесс для архитектора в корпоративном контуре:
//! TUI (ratatui), mermaid→ASCII рендер, якорные/динамические рубрики
//! архитектурного контроля, специализированные бенчмарки, MCP-интеграции,
//! веб-доступ к доменным знаниям, локальная база знаний, handoff-пакеты
//! кодовым харнессам (Claude Code, Qwen Code, `OpenClaw`, Hermes, Theseus,
//! `CodeWhale`), fitness functions и линтер architecture-spine, крон md-задач.
//!
//! Происхождение идей — `docs/SOURCE_BRIEF.md` (разбор SDD-харнессов и
//! корпоративных агентных фреймворков, август 2026).
//!
//! ```no_run
//! use arch_harness::config::Config;
//! let cfg = Config::load(None).expect("config");
//! assert!(cfg.models.contains_key("deepseek"));
//! ```

pub mod accept_gate;
pub mod adr_registry;
pub mod agent;
pub mod agentsmd;
pub mod archify;
pub mod archunit;
pub mod ariadna;
pub mod assets;
pub mod asyncapi;
pub mod bench;
pub mod clipboard;
pub mod computer;
pub mod concept;
pub mod config;
pub mod context_plan;
pub mod contract_diff;
pub mod control;
pub mod cron;
pub mod dataset_card;
pub mod delta;
pub mod detectors;
pub mod distill;
pub mod doctor;
pub mod error;
pub mod eval;
pub mod evidence;
pub mod evolve;
pub mod export;
pub mod failure_memory;
pub mod fleet;
pub mod fleet_exec;
pub mod fleet_gate;
pub mod fleet_plan;
pub mod fleet_run;
pub mod goal;
pub mod governance;
pub mod gpu;
pub mod harness;
pub mod hooks;
pub mod hypothesis;
pub mod injection;
pub mod kb;
pub mod landscape;
pub mod llm;
pub mod managed;
pub mod matchers;
pub mod mcp;
pub mod mcp_server;
pub mod memory;
pub mod mermaid;
pub mod metrics;
pub mod model;
pub mod net;
pub mod nfr;
pub mod openapi;
pub mod openspec;
pub mod plugin;
pub mod policy;
pub mod post_merge;
pub mod preflight;
pub mod provenance;
pub mod publish;
pub mod ralph;
pub mod rehearsal;
pub mod retry;
pub mod router;
pub mod rubric;
pub mod secrets;
pub mod subagent;
pub mod survey;
pub mod tool;
pub mod tools;
pub mod trace;
pub mod trajectory;
pub mod tui;
pub mod web;
pub mod weights;
pub mod worktree;

pub use config::Config;
pub use error::{HarnessError, Result};
