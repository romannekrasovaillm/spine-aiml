//! Детерминированный роутер доменных вопросов к экспертным моделям Ariadna.
//!
//! Разделение обязанностей (та же ось, что у флота и pre-flight): модель не
//! ГАДАЕТ, какой эксперт нужен, — роутер назначает. Сигнал трёхслойный:
//! 1. критичность вопроса (маркеры «обязательно/критично/перепроверь») → обе;
//! 2. редкий термин из war-chest-лексикона → v1;
//! 3. уровень концепта в базе Ариадны: `α`/`β` (базовый) → v10 (GRPO),
//!    `γ` (специализированный) или нечёткое совпадение → v1 (SFT).
//!
//! Не смогли детерминировать — честная эскалация к фронтиру, не гадание
//! (инвариант H0.4: низкая уверенность локальной модели → фронтир).
//!
//! КОНТРАКТ (владелец: модуль `ariadna`):
//! - [`route`] — чистая функция: вопрос + опциональная [`ConceptDb`] → [`ExpertRoute`];
//! - [`tools`] — инструмент `route_expert` (регистрируется при `[concept].enabled`).

use std::sync::Arc;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::concept::{ConceptDb, MatchKind};
use crate::error::Result;
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Экспертная модель Ariadna.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Expert {
    /// v10 GRPO — базовый/частотный термин.
    V10,
    /// v1 SFT — редкий/специализированный термин.
    V1,
    /// Обе модели — критичный вопрос (кросс-проверка v10→v1).
    Both,
    /// Эскалация к фронтиру (термин вне базы и лексикона).
    Frontier,
}

impl Expert {
    /// Ключ модели (для вывода и подсказки субагенту).
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::V10 => "v10",
            Self::V1 => "v1",
            Self::Both => "both",
            Self::Frontier => "frontier",
        }
    }
}

/// Результат роутинга: модель + аудируемая причина.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExpertRoute {
    /// Назначенная модель.
    pub expert: Expert,
    /// Почему так (аудируемость — как у [`crate::router::Assignment`]).
    pub reason: String,
}

/// Редкие термины war-chest (лексикон). Источник — память: RL-интерналы GRPO,
/// инфраструктурные ловушки GB10/vast.ai, узкие arXiv-2024-2026 концепты.
/// Совпадение — по слову (односложные) или подстроке (многословные).
const RARE_TERMS: &[&str] = &[
    "rlvr",
    "mode collapse",
    "multi-agent orchestration",
    "agent memory",
    "grafting",
    "self-play",
    "continual learning",
    "nvrm storm",
    "cxx11abi",
    "gpu0 tightrope",
    "nccl oom",
    "policy loss registry",
    "advantage estimator",
    "outer clip",
    "entropy bonus",
    "low_var_kl",
    "flashinfer",
];

/// Маркеры критичности вопроса (требуют кросс-проверки обеими моделями).
const CRITICAL_MARKERS: &[&str] = &[
    "обязательно",
    "критично",
    "критически",
    "перепроверь",
    "верифицируй",
    "точно ли",
];

/// Кириллические алиасы доменных терминов → канонические (англ.) термины
/// базы концептов (база англоязычная — без перевода русские запросы уходили
/// в frontier). Совпадение — по началу слова (морфология: «дистилляции»,
/// «графтингом»); стебли от 5 букв, чтобы не ловить бытовые слова.
const RU_ALIASES: &[(&str, &str)] = &[
    ("дистилля", "distillation"),
    ("графтинг", "grafting"),
    ("квантова", "quantization"),
    ("энтропи", "entropy"),
    ("внимание", "attention"),
    ("дообучен", "finetuning"),
    ("претрейн", "pretraining"),
    ("рассужд", "reasoning"),
    ("эмбеддинг", "embedding"),
    ("токениз", "tokenizer"),
    ("чекпоинт", "checkpoint"),
    ("датасет", "dataset"),
    ("бенчмарк", "benchmark"),
    ("градиент", "gradient"),
];

/// Перевести кириллические доменные термины вопроса в канонические (англ.)
/// по [`RU_ALIASES`]; остальные слова — без изменений. Разбиение — по
/// неалфавитным символам: дефисные слова («MoE-модель») делятся на токены
/// («moe», «модель»), а не склеиваются в одно («moeмодель»).
fn translate_ru(q: &str) -> String {
    q.split(|c: char| !c.is_alphanumeric())
        .filter(|w| !w.is_empty())
        .map(|w| {
            RU_ALIASES
                .iter()
                .find(|(stem, _)| w.starts_with(stem))
                .map_or(w, |(_, en)| en)
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Уровень концепта (α=базовый, β=промежуточный, γ=специализированный).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Level {
    Alpha,
    Beta,
    Gamma,
}

/// Разобрать строку уровня (Unicode `α`/`β`/`γ` или ASCII `alpha`/`beta`/`gamma`).
fn level_tag(s: &str) -> Option<Level> {
    match s.trim().to_lowercase().as_str() {
        "α" | "alpha" => Some(Level::Alpha),
        "β" | "beta" => Some(Level::Beta),
        "γ" | "gamma" => Some(Level::Gamma),
        _ => None,
    }
}

/// Детерминированный роутинг вопроса к экспертной модели Ariadna.
/// `db = None` — база концептов недоступна, роутинг по лексикону и критичности.
#[must_use]
pub fn route(question: &str, db: Option<&ConceptDb>) -> ExpertRoute {
    let q = question.to_lowercase();
    // Канонизированная форма: русские доменные термины переведены в английские,
    // дефисные слова разбиты на токены (лексикон и база — англоязычные).
    let qn = translate_ru(&q);

    // 1. Критичность → обе (кросс-проверка).
    if let Some(marker) = CRITICAL_MARKERS.iter().find(|m| q.contains(**m)) {
        return ExpertRoute {
            expert: Expert::Both,
            reason: format!("критичный вопрос (маркер «{marker}») — кросс-проверка v10→v1"),
        };
    }

    // 2. Редкий термин из лексикона → v1.
    if let Some(term) = RARE_TERMS.iter().find(|t| matches_term(&qn, t)) {
        return ExpertRoute {
            expert: Expert::V1,
            reason: format!("«{term}» — редкий термин (war-chest) → v1"),
        };
    }

    // 3. Сигнал базы концептов.
    if let Some(db) = db {
        for raw in qn.split_whitespace() {
            let word: String = raw
                .chars()
                .filter(|c| c.is_alphanumeric())
                .collect::<String>()
                .to_lowercase();
            if word.len() < 2 {
                continue;
            }
            let Some((kind, level)) = resolve_card(db, &word) else {
                continue;
            };
            if let Some(expert) = route_by_level(kind, level) {
                let reason = match expert {
                    Expert::V1 => format!("«{word}» — специализированный/нечёткий термин → v1"),
                    Expert::V10 => format!("«{word}» — базовый термин (α/β) → v10"),
                    _ => unreachable!("route_by_level не возвращает Both/Frontier"),
                };
                return ExpertRoute { expert, reason };
            }
        }
    }

    // 4. Не смогли детерминировать → эскалация к фронтиру.
    ExpertRoute {
        expert: Expert::Frontier,
        reason: "термин не в базе и не в лексиконе — эскалация к фронтиру".into(),
    }
}

/// Назначить модель по уровню концепта и типу совпадения.
fn route_by_level(kind: MatchKind, level: Option<&str>) -> Option<Expert> {
    // Нечёткое совпадение — термин неканонический/редкий → v1.
    if kind == MatchKind::Fuzzy {
        return Some(Expert::V1);
    }
    match level.and_then(level_tag) {
        Some(Level::Gamma) => Some(Expert::V1),
        Some(Level::Alpha | Level::Beta) => Some(Expert::V10),
        None => {
            // В базе, но уровень неизвестен: канонический slug/алиас → v10.
            matches!(
                kind,
                MatchKind::Slug | MatchKind::AliasExact | MatchKind::AliasFolded
            )
            .then_some(Expert::V10)
        }
    }
}

/// Разрешить слово в карточку концепта (тип совпадения + уровень).
fn resolve_card<'a>(db: &'a ConceptDb, word: &str) -> Option<(MatchKind, Option<&'a str>)> {
    let r = db.resolve(word);
    let slug = r.slug?;
    let card = db.lookup(&slug)?;
    Some((r.kind, card.level.as_deref()))
}

/// Совпадение термина: многословный/неалфавитный — подстрока, однословный — по слову.
fn matches_term(q: &str, term: &str) -> bool {
    if term.chars().any(|c| !c.is_alphanumeric()) {
        q.contains(term)
    } else {
        q.split(|c: char| !c.is_alphanumeric()).any(|w| w == term)
    }
}

// ─── Инструмент `route_expert` ─────────────────────────────────────────────

struct RouteExpertTool;

/// Аргументы инструмента `route_expert`.
#[derive(Deserialize)]
struct RouteArgs {
    question: String,
}

/// Инструменты домена: `route_expert` (регистрируется при `[concept].enabled`).
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(RouteExpertTool)]
}

#[async_trait]
impl Tool for RouteExpertTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "route_expert".into(),
            description: "Детерминированный выбор экспертной модели Ariadna для доменного ML/AI-вопроса: v10 (базовый термин), v1 (редкий), both (критичный), frontier (эскалация к фронтиру). Возвращает модель и причину — назначает роутер, а не модель.".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Один атомарный доменный вопрос (ML/AI-термин, архитектура, алгоритм)"}
                },
                "required": ["question"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: RouteArgs = serde_json::from_value(args).map_err(|e| {
            crate::error::HarnessError::Tool(format!("route_expert: невалидные аргументы: {e}"))
        })?;
        let index = &ctx.config.concept.index;
        let db = if index.as_os_str().is_empty() {
            None
        } else {
            // База недоступна — деградируем к лексикону/критичности, не падаем.
            ConceptDb::open_cached(index).ok()
        };
        let r = route(&args.question, db.as_deref());
        Ok(ToolOutput::ok(format!(
            "{} — {}",
            r.expert.as_str(),
            r.reason
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::concept::ConceptDb;

    /// Минимальная фикстура базы: α (attention), β (grpo), γ (rlvr).
    fn fixture_db() -> (ConceptDb, tempfile::TempDir) {
        let dir = tempfile::tempdir().expect("tmp");
        let index = dir.path().join("concept_index.json");
        let graph = dir.path().join("concept_graph.json");
        std::fs::write(
            &index,
            r#"{
                "concept_index": {
                    "concepts": {
                        "attention": {"type": "architectural_component", "file": "attention.md", "title": "Attention", "level": "α", "formality": "A", "family": "Transformers"},
                        "grpo": {"type": "algorithmic_primitive", "file": "grpo.md", "title": "GRPO", "level": "β", "formality": "B", "family": "RL Algorithms"},
                        "rlvr": {"type": "algorithmic_primitive", "file": "rlvr.md", "title": "RLVR", "level": "γ", "formality": "C", "family": "RL Algorithms"},
                        "distillation": {"type": "algorithmic_primitive", "file": "distillation.md", "title": "Distillation", "level": "β", "formality": "B", "family": "Training"}
                    },
                    "aliases": {"group_relative_policy_optimization": "grpo"}
                },
                "stats": {"total_concepts": 3}
            }"#,
        )
        .expect("write index");
        std::fs::write(&graph, r#"{"nodes": {}, "edges": []}"#).expect("write graph");
        let db = ConceptDb::load_paths(&index, &graph).expect("load");
        (db, dir)
    }

    #[test]
    fn foundational_term_routes_to_v10() {
        let (db, _d) = fixture_db();
        let r = route("что такое attention в трансформерах", Some(&db));
        assert_eq!(r.expert, Expert::V10);
        assert!(r.reason.contains("attention"));
    }

    #[test]
    fn gamma_term_routes_to_v1() {
        let (db, _d) = fixture_db();
        let r = route("объясни rlvr", Some(&db));
        assert_eq!(r.expert, Expert::V1);
    }

    #[test]
    fn rare_lexicon_term_routes_to_v1_without_db() {
        let r = route("как предотвратить mode collapse", None);
        assert_eq!(r.expert, Expert::V1);
        assert!(r.reason.contains("mode collapse"));
    }

    #[test]
    fn critical_marker_routes_to_both() {
        let (db, _d) = fixture_db();
        let r = route("обязательно перепроверь что такое grpo", Some(&db));
        assert_eq!(r.expert, Expert::Both);
    }

    #[test]
    fn unknown_term_escalates_to_frontier() {
        let (db, _d) = fixture_db();
        let r = route("что такое flurbo-baz", Some(&db));
        assert_eq!(r.expert, Expert::Frontier);
    }

    #[test]
    fn fuzzy_match_routes_to_v1() {
        let (db, _d) = fixture_db();
        // «attentio» — нечёткое совпадение с attention → v1 (неканонический).
        let r = route("что делает attentio", Some(&db));
        assert_eq!(r.expert, Expert::V1);
    }

    #[test]
    fn short_lexicon_word_matches_by_boundary() {
        // «cxx11abi» — слово, не должно ловиться в «stability».
        let r = route("что такое cxx11abi", None);
        assert_eq!(r.expert, Expert::V1);
        let r2 = route("стабильность обучения", None);
        assert_eq!(r2.expert, Expert::Frontier);
    }

    #[test]
    fn russian_aliases_translate_and_route() {
        let (db, _d) = fixture_db();
        // «дистилляция» → distillation (β в базе) → v10; раньше — frontier.
        let r = route("дистилляция рассуждений из большой модели", Some(&db));
        assert_eq!(r.expert, Expert::V10, "{r:?}");
        assert!(r.reason.contains("distillation"), "{r:?}");
        // «графтинг» → grafting (редкий термин лексикона) → v1, база не нужна.
        let r = route("как делать графтинг двух моделей", None);
        assert_eq!(r.expert, Expert::V1);
        assert!(r.reason.contains("grafting"), "{r:?}");
        // Морфология: «квантованием» → quantization (в фикстуре нет) → frontier.
        let r = route("квантованием весов", None);
        assert_eq!(r.expert, Expert::Frontier);
        // Нормализация сама по себе (стебли от 5 букв, бытовые слова целы).
        assert_eq!(
            translate_ru("moe-модель: графтингом и дистилляции"),
            "moe модель grafting и distillation"
        );
    }

    #[test]
    fn hyphenated_words_split_into_tokens() {
        let (db, _d) = fixture_db();
        // «rlvr-среды» делится на «rlvr» + «среды»: γ-термин находится (v1);
        // склейка «rlvrсреды» уходила бы в frontier.
        let r = route("что такое rlvr-среды", Some(&db));
        assert_eq!(r.expert, Expert::V1, "{r:?}");
    }
}
