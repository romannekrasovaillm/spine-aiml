//! Детерминированный роутер флота (ось «детерминированный контроль»).
//!
//! Назначение work-items агентам — ЧИСТАЯ функция: без LLM, без случайности,
//! без побочных эффектов. Разделение обязанностей: модель декомпозирует пакет
//! в [`WorkItem`]'ы, [`Router`] назначает их агентам по скорингу. Выход —
//! аудируемая карта `item → agent` со скором и причинами (кто, почему, какой
//! скор) — то, чего нет у вендорских флотов из N независимых процессов.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::control::Route;

/// Тип агента флота.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum AgentKind {
    /// Кодовый харнесс (субпроцесс: claude-code, qwen-code, …).
    Harness,
    /// In-process субагент (LLM-специализация из `plugins/*/agents/*.md`).
    Subagent,
}

/// Тир стоимости агента (дешёвый → дорогой).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum CostTier {
    Cheap,
    Standard,
    Expensive,
}

/// Профиль агента флота (материал для скоринга назначения).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentProfile {
    /// Идентификатор (имя харнесса или имя спеки субагента).
    pub id: String,
    /// Тип агента.
    pub kind: AgentKind,
    /// Скиллы (теги компетенций, напр. `rust`, `async`, `ml-concept`).
    pub skills: Vec<String>,
    /// Инструменты (имена из реестра).
    pub tools: Vec<String>,
    /// Теги домена/аффинити (напр. `code`, `docs`, `ml-concept`).
    pub tags: Vec<String>,
    /// Текущая нагрузка (сколько items уже назначено).
    pub load: usize,
    /// Ёмкость (максимум параллельных items).
    pub capacity: usize,
    /// Тир стоимости.
    pub cost_tier: CostTier,
}

/// Work-item — единица декомпозиции handoff-пакета.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkItem {
    /// Идентификатор (стабильный, напр. `ADR-014`, `spine-модуль-<n>`).
    pub id: String,
    /// Текст задания (что сделать).
    pub spec: String,
    /// Требуемые скиллы для выполнения.
    pub required_skills: Vec<String>,
    /// Теги предмета (домен/характер).
    pub tags: Vec<String>,
    /// Маршрут значимости (риск).
    pub route: Route,
    /// Домен аффинити (опционально: `code`, `docs`, `ml-concept`, …).
    pub domain: Option<String>,
    /// Относительная трудоёмкость (для порядка назначения).
    pub effort: usize,
}

/// Назначение item → agent с причиной (аудируемо).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Assignment {
    /// Идентификатор work-item.
    pub item_id: String,
    /// Идентификатор агента.
    pub agent_id: String,
    /// Итоговый скор (чем выше, тем лучше соответствие).
    pub score: f64,
    /// Причины (компоненты скора) для аудита.
    pub reason: Vec<String>,
}

/// План маршрутизации: назначения + нераспределённые items.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RoutePlan {
    /// Назначенные items (в детерминированном порядке).
    pub assignments: Vec<Assignment>,
    /// Items, которым не хватило ёмкости ни одного агента.
    pub unassigned: Vec<String>,
}

/// Веса компонентов скоринга (сумма нормирована вызывающим в конфиге;
/// роутер тупо взвешивает — сумма ≠ 1 допустима, важны относительные веса).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RouterWeights {
    /// Пересечение `required_skills` × skills.
    pub capability: f64,
    /// Наименее загруженный агент.
    pub load: f64,
    /// Домен-аффинити (item.domain × agent.tags).
    pub affinity: f64,
    /// Стоимость по маршруту (Fast → дешёвый).
    pub cost: f64,
    /// Риск маршрута (Critical → только харнесс).
    pub route_risk: f64,
}

impl Default for RouterWeights {
    fn default() -> Self {
        Self {
            capability: 1.0,
            load: 0.6,
            affinity: 0.8,
            cost: 0.5,
            route_risk: 2.0, // Critical → harness — жёсткое правило, перевешивает
        }
    }
}

/// Детерминированный роутер.
#[derive(Debug, Clone, Default)]
pub struct Router {
    /// Веса скоринга.
    pub weights: RouterWeights,
}

impl Router {
    /// Роутер с весами по умолчанию.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Роутер с явными весами.
    #[must_use]
    pub fn with_weights(weights: RouterWeights) -> Self {
        Self { weights }
    }

    /// Скор одного item на одном агенте при текущей нагрузке `cur_load`.
    /// Возвращает (скор, причины).
    fn score(&self, item: &WorkItem, agent: &AgentProfile, cur_load: usize) -> (f64, Vec<String>) {
        let mut reasons = Vec::new();

        // 1. Capability: доля требуемых скиллов, покрытых агентом.
        let cap = if item.required_skills.is_empty() {
            0.5 // нейтрально: требований нет — любой скилл подходит
        } else {
            let matched = item
                .required_skills
                .iter()
                .filter(|s| agent.skills.iter().any(|a| a == *s))
                .count();
            reasons.push(format!(
                "capability {matched}/{} skills",
                item.required_skills.len()
            ));
            matched as f64 / item.required_skills.len() as f64
        };

        // 2. Load: наименее загруженный. Ёмкость исчерпана — агент исключён
        //    вызывающим до скоринга, здесь cur_load < capacity всегда.
        let load = 1.0 - (cur_load as f64 / agent.capacity.max(1) as f64);
        reasons.push(format!("load {cur_load}/{}", agent.capacity));

        // 3. Affinity: домен item попал в теги агента.
        let (affinity, aff_note) = match &item.domain {
            Some(d) => {
                let hit = agent.tags.iter().any(|t| t == d);
                (
                    if hit { 1.0 } else { 0.0 },
                    format!("affinity {d}:{}", if hit { "hit" } else { "miss" }),
                )
            }
            None => (0.5, "affinity —".into()),
        };
        reasons.push(aff_note);

        // 4. Cost: дешёвый агент для Fast, сбалансированный для Standard,
        //    безразличен на Critical (там важна надёжность, не цена).
        let cost = match item.route {
            Route::Fast => match agent.cost_tier {
                CostTier::Cheap => 1.0,
                CostTier::Standard => 0.5,
                CostTier::Expensive => 0.0,
            },
            Route::Standard => match agent.cost_tier {
                CostTier::Cheap => 0.7,
                CostTier::Standard => 1.0,
                CostTier::Expensive => 0.3,
            },
            Route::Critical => 1.0,
        };
        reasons.push(format!("cost {:?}@{:?}", agent.cost_tier, item.route));

        // 5. Route-риск: Critical — только кодовый харнесс (строгий таймаут +
        //    человеко-гейт); субагент на Critical — жёсткий штраф.
        let route_risk = match item.route {
            Route::Critical => match agent.kind {
                AgentKind::Harness => 1.0,
                AgentKind::Subagent => 0.0,
            },
            _ => 1.0,
        };
        reasons.push(format!("route {agent:?}", agent = agent.kind));

        let w = &self.weights;
        let score = w.capability * cap
            + w.load * load
            + w.affinity * affinity
            + w.cost * cost
            + w.route_risk * route_risk;
        (score, reasons)
    }

    /// Назначает items агентам детерминированно.
    ///
    /// Порядок items: Critical раньше, затем по трудоёмкости (тяжёлые раньше),
    /// затем по id (стабильный tie-break). Жадный выбор: каждый item — агенту
    /// с максимальным скором среди тех, у кого есть свободная ёмкость.
    ///
    /// # Panics
    /// Практически недостижимо: инвариант «агент из лучшего скора присутствует
    /// в карте загрузки» (карта построена из того же списка агентов).
    #[must_use]
    pub fn assign(&self, items: &[WorkItem], agents: &[AgentProfile]) -> RoutePlan {
        let mut order: Vec<&WorkItem> = items.iter().collect();
        order.sort_by(|a, b| {
            route_rank(b.route)
                .cmp(&route_rank(a.route))
                .then(b.effort.cmp(&a.effort))
                .then(a.id.cmp(&b.id))
        });

        let mut loads: HashMap<&str, usize> =
            agents.iter().map(|a| (a.id.as_str(), a.load)).collect();
        let mut assignments = Vec::new();
        let mut unassigned = Vec::new();

        for item in order {
            let mut best: Option<(String, f64, Vec<String>)> = None;
            for agent in agents {
                let cur = loads[agent.id.as_str()];
                if cur >= agent.capacity {
                    continue;
                }
                let (s, reasons) = self.score(item, agent, cur);
                let better = match &best {
                    None => true,
                    Some((_, bs, _)) => {
                        s > *bs || (s == *bs && agent.id < best.as_ref().unwrap().0)
                    }
                };
                if better {
                    best = Some((agent.id.clone(), s, reasons));
                }
            }
            match best {
                Some((agent_id, score, reason)) => {
                    *loads.get_mut(agent_id.as_str()).expect("агент в карте") += 1;
                    assignments.push(Assignment {
                        item_id: item.id.clone(),
                        agent_id,
                        score,
                        reason,
                    });
                }
                None => unassigned.push(item.id.clone()),
            }
        }

        RoutePlan {
            assignments,
            unassigned,
        }
    }
}

/// Ранг маршрута для приоритизации (Critical первым).
fn route_rank(route: Route) -> u8 {
    match route {
        Route::Critical => 2,
        Route::Standard => 1,
        Route::Fast => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn agent(
        id: &str,
        kind: AgentKind,
        skills: &[&str],
        tags: &[&str],
        capacity: usize,
        cost: CostTier,
    ) -> AgentProfile {
        AgentProfile {
            id: id.into(),
            kind,
            skills: skills
                .iter()
                .map(std::string::ToString::to_string)
                .collect(),
            tools: Vec::new(),
            tags: tags.iter().map(std::string::ToString::to_string).collect(),
            load: 0,
            capacity,
            cost_tier: cost,
        }
    }

    fn item(
        id: &str,
        route: Route,
        skills: &[&str],
        domain: Option<&str>,
        effort: usize,
    ) -> WorkItem {
        WorkItem {
            id: id.into(),
            spec: id.into(),
            required_skills: skills
                .iter()
                .map(std::string::ToString::to_string)
                .collect(),
            tags: Vec::new(),
            route,
            domain: domain.map(str::to_owned),
            effort,
        }
    }

    #[test]
    fn capability_steers_toward_matching_agent() {
        let r = Router::new();
        let items = vec![item("i1", Route::Fast, &["rust"], None, 1)];
        let agents = vec![
            agent(
                "a",
                AgentKind::Subagent,
                &["rust", "async"],
                &["code"],
                4,
                CostTier::Cheap,
            ),
            agent(
                "b",
                AgentKind::Subagent,
                &["python"],
                &["code"],
                4,
                CostTier::Cheap,
            ),
        ];
        let plan = r.assign(&items, &agents);
        assert_eq!(plan.assignments[0].agent_id, "a");
        assert!(plan.unassigned.is_empty());
    }

    #[test]
    fn critical_route_requires_harness_not_subagent() {
        let r = Router::new();
        let items = vec![item("c1", Route::Critical, &[], None, 1)];
        // Суб-агент дешевле и нагружен одинаково, но Critical → только harness.
        let agents = vec![
            agent("sub", AgentKind::Subagent, &[], &[], 4, CostTier::Cheap),
            agent(
                "harness",
                AgentKind::Harness,
                &[],
                &[],
                4,
                CostTier::Expensive,
            ),
        ];
        let plan = r.assign(&items, &agents);
        assert_eq!(plan.assignments[0].agent_id, "harness");
    }

    #[test]
    fn capacity_exhausted_agent_is_skipped() {
        let r = Router::new();
        let items = vec![
            item("i1", Route::Fast, &[], None, 1),
            item("i2", Route::Fast, &[], None, 1),
            item("i3", Route::Fast, &[], None, 1),
        ];
        let agents = vec![
            agent("a", AgentKind::Subagent, &[], &[], 1, CostTier::Cheap),
            agent("b", AgentKind::Subagent, &[], &[], 1, CostTier::Cheap),
        ];
        let plan = r.assign(&items, &agents);
        // Суммарная ёмкость 1+1 = 2, items три — третий некуда.
        assert_eq!(plan.assignments.len(), 2);
        assert_eq!(plan.unassigned, vec!["i3"]);
    }

    #[test]
    fn critical_items_scheduled_first() {
        let r = Router::new();
        let items = vec![
            item("fast-1", Route::Fast, &[], None, 100),
            item("crit-1", Route::Critical, &[], None, 1),
        ];
        let agents = vec![agent(
            "h",
            AgentKind::Harness,
            &[],
            &[],
            1,
            CostTier::Expensive,
        )];
        let plan = r.assign(&items, &agents);
        // Critical назначен первым (ему досталась единственная ёмкость),
        // Fast остался нераспределённым.
        assert_eq!(plan.assignments[0].item_id, "crit-1");
        assert_eq!(plan.unassigned, vec!["fast-1"]);
    }

    #[test]
    fn assignment_reason_is_auditable() {
        let r = Router::new();
        let items = vec![item("i1", Route::Fast, &["rust"], Some("code"), 1)];
        let agents = vec![agent(
            "a",
            AgentKind::Subagent,
            &["rust"],
            &["code"],
            4,
            CostTier::Cheap,
        )];
        let plan = r.assign(&items, &agents);
        let a = &plan.assignments[0];
        assert!(!a.reason.is_empty(), "причины для аудита обязательны");
        assert!(a.reason.iter().any(|s| s.contains("capability 1/1")));
        assert!(a.reason.iter().any(|s| s.contains("affinity code:hit")));
    }
}
