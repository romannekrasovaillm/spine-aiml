//! Фазный компилятор контекста (H1.3) + capability-seams (H1.4).
//!
//! H1.3: `significance → фаза → скиллы/правила → бюджет` выносится в явный,
//! детерминированный компонент — [`compile_context`]. Сейчас эта связь
//! неявная (маршрут Fast/Standard/Critical влияет на промпт опосредованно).
//!
//! H1.4: capability-seams — заменяемые слои харнесса за трейтами/реестрами
//! ([`SEAMS`]); инвариант «model-visible ⟺ logged» держит событийный журнал.

use crate::control::Route;

/// Фаза архитектурной работы.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContextPhase {
    /// Оценка значимости/рисков.
    Assessment,
    /// Проектирование решения.
    Design,
    /// Передача контекста исполнителю (handoff).
    Handoff,
}

impl ContextPhase {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Assessment => "assessment",
            Self::Design => "design",
            Self::Handoff => "handoff",
        }
    }
}

/// План контекста: маршрут → фазы → скиллы/правила → бюджет окна.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextPlan {
    /// Маршрут значимости.
    pub route: Route,
    /// Фазы работы (по порядку).
    pub phases: Vec<ContextPhase>,
    /// Доменные скиллы фазы (имена).
    pub skills: Vec<&'static str>,
    /// Fitness-правила, обязательные на маршруте (id).
    pub rules: Vec<&'static str>,
    /// Рекомендуемый бюджет окна, токены.
    pub budget: usize,
}

/// Capability-seams харнесса (H1.4) — заменяемые слои за трейтами/реестрами.
pub const SEAMS: &[&str] = &[
    "llm",
    "tool",
    "kb",
    "web",
    "mcp",
    "concept",
    "governance",
    "evolve",
    "distill",
];

/// Компилирует план контекста по маршруту значимости (детерминированно).
#[must_use]
pub fn compile_context(route: Route) -> ContextPlan {
    match route {
        Route::Fast => ContextPlan {
            route,
            phases: vec![ContextPhase::Design],
            skills: vec!["adr-authoring", "fitness-functions"],
            rules: vec!["no_unsafe_code", "msrv_pinned"],
            budget: 8_000,
        },
        Route::Standard => ContextPlan {
            route,
            phases: vec![ContextPhase::Assessment, ContextPhase::Design],
            skills: vec![
                "adr-authoring",
                "fitness-functions",
                "c4-mermaid",
                "handoff-packaging",
            ],
            rules: vec!["no_unsafe_code", "msrv_pinned", "address_trace"],
            budget: 16_000,
        },
        Route::Critical => ContextPlan {
            route,
            phases: vec![
                ContextPhase::Assessment,
                ContextPhase::Design,
                ContextPhase::Handoff,
            ],
            skills: vec![
                "adr-authoring",
                "fitness-functions",
                "c4-mermaid",
                "handoff-packaging",
                "rubric-judging",
                "readiness-gate",
            ],
            rules: vec![
                "no_unsafe_code",
                "msrv_pinned",
                "address_trace",
                "spine_invariants",
            ],
            budget: 32_000,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fast_route_is_minimal() {
        let p = compile_context(Route::Fast);
        assert_eq!(p.phases, vec![ContextPhase::Design]);
        assert_eq!(p.skills.len(), 2);
        assert_eq!(p.budget, 8_000);
    }

    #[test]
    fn critical_route_has_handoff_and_larger_budget() {
        let p = compile_context(Route::Critical);
        assert!(p.phases.contains(&ContextPhase::Handoff));
        assert!(p.budget > compile_context(Route::Fast).budget);
        assert!(p.skills.len() > compile_context(Route::Fast).skills.len());
    }

    #[test]
    fn seams_cover_domain_modules() {
        assert!(SEAMS.contains(&"concept"));
        assert!(SEAMS.contains(&"governance"));
        assert!(SEAMS.contains(&"evolve"));
    }
}
