//! Ядерные инструменты: bash и файловые операции.
//!
//! КОНТРАКТ (владелец: агент `tools`):
//! - [`bash`] — выполнение shell-команд с таймаутом и лимитом вывода;
//! - [`fs`] — read/write/edit/glob/grep;
//! - [`ask`] — интерактивный выбор вариантов пользователем (`propose_options`);
//! - [`core_registry`] — реестр ядерных инструментов;
//! - [`full_registry`] — ядро + доменные инструменты (`mermaid::tools()`,
//!   `rubric::tools()`, `web::tools()`, `kb::tools()`, `control::tools()`,
//!   `openapi::tools()`, `model::tools()`, `trace::tools()`, `harness::tools()`,
//!   `fleet`).

use std::sync::Arc;

use crate::config::Config;
use crate::tool::{Tool, ToolRegistry};

pub mod ask;
pub mod bash;
pub mod fs;
pub mod screenshot;

/// Реестр ядерных инструментов: bash, файлы, glob/grep, `propose_options`,
/// наблюдение за экраном (скриншот/чтение изображения, размер экрана, список
/// окон — нативная мультимодальность `deepseek-flash`).
///
/// Наблюдение включено всегда: видеть экран безопасно. Воздействие
/// (`computer_*`, `browser_*`) живёт в [`crate::computer`] и подключается
/// доменным реестром под гейтом конфига (ADR-041).
#[must_use]
pub fn core_registry() -> ToolRegistry {
    let mut reg = ToolRegistry::new()
        .with(Arc::new(bash::BashTool))
        .with(Arc::new(fs::ReadFileTool))
        .with(Arc::new(fs::WriteFileTool))
        .with(Arc::new(fs::EditFileTool))
        .with(Arc::new(fs::GlobTool))
        .with(Arc::new(fs::GrepTool))
        .with(Arc::new(ask::ProposeOptionsTool));
    for tool in screenshot::tools() {
        reg.register(tool);
    }
    reg
}

/// Полный реестр: ядро + специализированные инструменты архитектора.
/// Политика автономии — из `Config::policy` (R-уровни).
#[must_use]
pub fn full_registry(cfg: &Config) -> ToolRegistry {
    let mut reg = core_registry();
    for tool in domain_tools(cfg) {
        reg.register(tool);
    }
    let policy = crate::policy::Policy::parse(&cfg.policy.autonomy).unwrap_or_default();
    reg.with_policy(policy)
}

fn domain_tools(cfg: &Config) -> Vec<Arc<dyn Tool>> {
    let mut out: Vec<Arc<dyn Tool>> = Vec::new();
    out.extend(crate::mermaid::tools());
    // Archify-контур диаграмм (AD-1): при `[archify].enabled = false`
    // инструменты не регистрируются — гейт на уровне регистрации (как web).
    if cfg.archify.enabled {
        out.extend(crate::archify::tools());
    }
    // Управление компьютером и браузером (ADR-041): воздействие на реальную
    // машину — опт-ин. При `[computer].enabled = false` (дефолт) инструменты
    // не регистрируются вовсе; наблюдение (`screenshot`, `screen_size`,
    // `window_list`) при этом остаётся в ядре и включено всегда.
    out.extend(crate::computer::tools(cfg));
    out.extend(crate::rubric::tools());
    // Egress-дисциплина (AD-BE5, GAP-C1): при `[web].enabled = false` веб-канал
    // выключен конфигом — инструменты не регистрируются, агент их не видит.
    // Гейт живёт на уровне регистрации, сами web-инструменты о нём не знают (AD-4).
    if cfg.web.enabled {
        out.extend(crate::web::tools());
    }
    // Доменная база концептов (Ариадна): `concept_search` — внешняя база,
    // включается явно пресетом (`[concept].enabled = true`).
    if cfg.concept.enabled {
        out.extend(crate::concept::tools());
        out.extend(crate::ariadna::tools());
    }
    out.extend(crate::kb::tools());
    out.extend(crate::control::tools());
    out.extend(crate::openapi::tools());
    out.extend(crate::asyncapi::tools());
    out.extend(crate::contract_diff::tools());
    out.extend(crate::model::tools());
    out.extend(crate::trace::tools());
    out.extend(crate::harness::tools(cfg));
    out.extend(crate::plugin::tools(cfg));
    out.extend(crate::agentsmd::tools(cfg));
    out.extend(crate::subagent::tools(cfg));
    out.extend(crate::ralph::tools(cfg));
    out.push(Arc::new(crate::worktree::WorktreeNewTool));
    out.push(Arc::new(crate::fleet::FleetAuditTool));
    out.push(Arc::new(crate::fleet_run::FleetRunTool));
    out.push(Arc::new(crate::fleet_exec::FleetPlanTool));
    out.extend(crate::survey::tools());
    out.extend(crate::distill::tools(cfg));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_registry_contains_core_interaction_and_domain_tools() {
        let cfg = Config::default();
        let names = full_registry(&cfg).names();
        for expected in [
            "bash",
            "read_file",
            "propose_options",
            "subagent_run",
            "subagent_list",
            "subagent_result",
            "ralph_run",
            "worktree_new",
            "fleet_audit",
            "fleet_run",
            "fleet_plan",
            "reverse_survey",
            "skill_distill",
            "skill_search",
            "mermaid_render",
            "archify_validate",
            "archify_deliver",
            "archify_show",
            "archify_compare",
            "model_query",
            "trace_check",
            "openapi_lint",
            "asyncapi_lint",
            "contract_diff",
        ] {
            assert!(
                names.iter().any(|n| n == expected),
                "нет инструмента {expected}"
            );
        }
    }

    #[test]
    fn full_registry_omits_computer_tools_by_default() {
        // ADR-041: воздействие на реальную машину — опт-ин. Дефолт
        // `[computer].enabled = false` не регистрирует ни одного инструмента
        // ввода, включая браузерный подконтур.
        let cfg = Config::default();
        assert!(!cfg.computer.enabled, "дефолт конфига — выключено");
        let names = full_registry(&cfg).names();
        for banned in [
            "computer_click",
            "computer_move",
            "computer_type",
            "browser_navigate",
            "browser_launch",
        ] {
            assert!(
                !names.iter().any(|n| n == banned),
                "инструмент {banned} не должен регистрироваться при [computer].enabled=false"
            );
        }
    }

    #[test]
    fn full_registry_keeps_observation_tools_when_computer_disabled() {
        // Наблюдение — ядро, гейтом воздействия не управляется: видеть экран
        // безопасно, поэтому screenshot/screen_size/window_list есть всегда.
        let cfg = Config::default();
        let names = full_registry(&cfg).names();
        for expected in ["screenshot", "read_image", "screen_size", "window_list"] {
            assert!(
                names.iter().any(|n| n == expected),
                "наблюдение {expected} должно быть в ядре при выключенном воздействии"
            );
        }
    }

    #[test]
    fn full_registry_keeps_computer_tools_when_enabled() {
        let mut cfg = Config::default();
        cfg.computer.enabled = true;
        let names = full_registry(&cfg).names();
        for expected in [
            "computer_click",
            "computer_move",
            "computer_key",
            "window_focus",
        ] {
            assert!(
                names.iter().any(|n| n == expected),
                "нет инструмента {expected} при [computer].enabled=true"
            );
        }
        assert!(
            !names.iter().any(|n| n == "browser_navigate"),
            "браузерный подконтур включается отдельно ([computer.browser].enabled)"
        );
    }

    #[test]
    fn full_registry_adds_browser_tools_when_subcontour_enabled() {
        let mut cfg = Config::default();
        cfg.computer.enabled = true;
        cfg.computer.browser.enabled = true;
        let names = full_registry(&cfg).names();
        for expected in ["browser_navigate", "browser_screenshot", "browser_eval"] {
            assert!(
                names.iter().any(|n| n == expected),
                "нет инструмента {expected} при [computer.browser].enabled=true"
            );
        }
    }

    #[test]
    fn full_registry_omits_web_tools_when_web_disabled() {
        // GAP-C1: при `[web].enabled = false` веб-инструменты отсутствуют в
        // инструментарии сессии — агент их не видит (egress-дисциплина, AD-BE5).
        let mut cfg = Config::default();
        cfg.web.enabled = false;
        let names = full_registry(&cfg).names();
        for banned in ["web_search", "web_fetch", "web_arch_sites"] {
            assert!(
                !names.iter().any(|n| n == banned),
                "инструмент {banned} не должен регистрироваться при [web].enabled=false"
            );
        }
    }

    #[test]
    fn full_registry_keeps_web_tools_by_default() {
        // Поведение по умолчанию не меняется: без ключа enabled веб-инструменты
        // регистрируются как раньше.
        let cfg = Config::default();
        let names = full_registry(&cfg).names();
        for expected in ["web_search", "web_fetch", "web_arch_sites"] {
            assert!(
                names.iter().any(|n| n == expected),
                "нет веб-инструмента {expected} при дефолтном конфиге"
            );
        }
    }

    #[test]
    fn full_registry_omits_concept_tool_by_default() {
        // База концептов — внешняя машинно-специфичная: по умолчанию
        // `[concept].enabled = false`, инструмент не регистрируется.
        let cfg = Config::default();
        let names = full_registry(&cfg).names();
        assert!(
            !names.iter().any(|n| n == "concept_search"),
            "concept_search не должен регистрироваться при [concept].enabled=false"
        );
    }

    #[test]
    fn full_registry_keeps_concept_tool_when_enabled() {
        let mut cfg = Config::default();
        cfg.concept.enabled = true;
        cfg.concept.index = std::path::PathBuf::from("/tmp/concept_index.json");
        let names = full_registry(&cfg).names();
        assert!(
            names.iter().any(|n| n == "concept_search"),
            "нет concept_search при [concept].enabled=true"
        );
    }

    #[test]
    fn full_registry_omits_archify_tools_when_archify_disabled() {
        // Гейт регистрации (как у web): при `[archify].enabled = false`
        // archify-инструменты отсутствуют в инструментарии сессии.
        let mut cfg = Config::default();
        cfg.archify.enabled = false;
        let names = full_registry(&cfg).names();
        for banned in [
            "archify_validate",
            "archify_deliver",
            "archify_show",
            "archify_compare",
        ] {
            assert!(
                !names.iter().any(|n| n == banned),
                "инструмент {banned} не должен регистрироваться при [archify].enabled=false"
            );
        }
    }
}
