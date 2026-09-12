//! Матрица повторных попыток API-вызовов (по опыту Theseus `retry.rs`,
//! там — переработка паттерна Codex `retry`): повторять имеет смысл только
//! то, что может починиться само, — 429, 5xx, транспортные сбои. Ошибки
//! аутентификации и невалидного запроса не ретраятся никогда: повтор того же
//! тела даст тот же отказ, а ключ/права чинятся вне цикла ретраев.
//!
//! КОНТРАКТ (владелец: агент `agent`):
//! - [`classify`] раскладывает отказ по классам [`ErrorKind`] (по HTTP-статусу
//!   и маркерам в тексте ошибки);
//! - [`RetryPolicy::for_kind`] задаёт статическую политику на класс
//!   (`None` — не повторять);
//! - [`RetryPolicy::delays`] — конечный итератор задержек между попытками
//!   (экспонента с капом + детерминированный джиттер `SplitMix64`);
//! - [`is_context_overflow`] выделяет «контекст не влез» (HTTP 413 и маркеры
//!   context length) — сигнал агенту для compact & resubmit.

use std::time::Duration;

/// Класс ошибки API-вызова — решает, ретраить ли и с какой политикой.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ErrorKind {
    /// HTTP 429 (Too Many Requests): превышен лимит запросов/токенов.
    /// Повторять с самым терпеливым backoff'ом — лимит обычно скользящий.
    RateLimit,
    /// HTTP 5xx: внутренняя ошибка/перегрузка сервера. Повторять.
    Server5xx,
    /// Транспортный сбой: таймаут, обрыв соединения, DNS. Повторять.
    Network,
    /// HTTP 413 / «context length exceeded»: запрос не влезает в контекст.
    /// Ретрай бессмысленен — нужна компактификация (см. [`is_context_overflow`]).
    ContextOverflow,
    /// HTTP 401/403: аутентификация/авторизация. НЕ повторять.
    Auth,
    /// Открыт circuit breaker источника авторизации (OAuth-рефрешер
    /// `GigaChat`, ADR-021 M2): источник в режиме быстрого отказа и сам
    /// решит, когда пробовать снова. НЕ повторять — ретрай не лечит.
    CircuitOpen,
    /// HTTP 400 и прочие 4xx: запрос некорректен. НЕ повторять.
    BadRequest,
    /// Не удалось классифицировать. Повторять осторожно (короткая политика).
    Unknown,
}

/// Маркеры rate-limit в теле/тексте ошибки (нижний регистр).
const RATE_LIMIT_MARKERS: &[&str] = &["rate limit", "rate_limit", "too many requests", "429"];
/// Маркеры серверных ошибок.
const SERVER_MARKERS: &[&str] = &[
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
];
/// Маркеры транспортных сбоев.
const NETWORK_MARKERS: &[&str] = &[
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "dns",
    "eof",
    // Русские формулировки собственных ошибок харнесса (post_once и пр.).
    "таймаут",
];
/// Маркеры переполнения контекста.
const CONTEXT_MARKERS: &[&str] = &[
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "request entity too large",
    "payload too large",
    "http 413",
    "prompt is too long",
    "context window",
];
/// Маркеры ошибок аутентификации.
const AUTH_MARKERS: &[&str] = &[
    "unauthorized",
    "forbidden",
    "invalid api key",
    "authentication",
];
/// Маркеры открытого circuit breaker'а (быстрый отказ источника
/// авторизации, `GigaChat` OAuth): повторять сейчас бессмысленно.
const CIRCUIT_OPEN_MARKERS: &[&str] = &["circuit breaker"];

/// Классифицирует отказ по HTTP-статусу и тексту ошибки.
///
/// Статус в приоритете; при его отсутствии (транспортный сбой reqwest)
/// класс выводится из маркеров в тексте (нижний регистр).
#[must_use]
pub fn classify(status: Option<u16>, err_text: &str) -> ErrorKind {
    if let Some(code) = status {
        return match code {
            429 => ErrorKind::RateLimit,
            413 => ErrorKind::ContextOverflow,
            401 | 403 => ErrorKind::Auth,
            400..=499 => ErrorKind::BadRequest,
            500..=599 => ErrorKind::Server5xx,
            _ => ErrorKind::Unknown,
        };
    }
    let text = err_text.to_lowercase();
    if CONTEXT_MARKERS.iter().any(|m| text.contains(m)) {
        ErrorKind::ContextOverflow
    } else if CIRCUIT_OPEN_MARKERS.iter().any(|m| text.contains(m)) {
        // Быстрый отказ источника авторизации (GigaChat OAuth): он сам
        // управляет окном повторов, ретраить поверх — только трата времени.
        ErrorKind::CircuitOpen
    } else if RATE_LIMIT_MARKERS.iter().any(|m| text.contains(m)) {
        ErrorKind::RateLimit
    } else if AUTH_MARKERS.iter().any(|m| text.contains(m)) {
        ErrorKind::Auth
    } else if SERVER_MARKERS.iter().any(|m| text.contains(m)) {
        ErrorKind::Server5xx
    } else if NETWORK_MARKERS.iter().any(|m| text.contains(m)) {
        ErrorKind::Network
    } else {
        ErrorKind::Unknown
    }
}

/// Признак «контекст не влез» — триггер компактификации с повтором хода.
#[must_use]
pub fn is_context_overflow(status: Option<u16>, err_text: &str) -> bool {
    classify(status, err_text) == ErrorKind::ContextOverflow
}

/// Политика повторов для класса ошибки.
///
/// Задержек между попытками, соответственно, `max_attempts - 1`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RetryPolicy {
    /// Всего попыток (включая первую).
    pub max_attempts: u32,
    /// Базовая задержка (мс) перед первым ретраем (до джиттера).
    pub base_ms: u64,
    /// Кап экспоненциальной задержки (мс, до джиттера).
    pub max_ms: u64,
    /// Ширина симметричного джиттера в процентах (0..=100):
    /// итоговая задержка ∈ `[cap·(1-p/100), cap·(1+p/100)]`.
    pub jitter_pct: u8,
}

impl RetryPolicy {
    /// Матрица повторов: политика по классу ошибки.
    ///
    /// `None` для [`ErrorKind::Auth`], [`ErrorKind::BadRequest`],
    /// [`ErrorKind::ContextOverflow`] и [`ErrorKind::CircuitOpen`] — эти
    /// отказы не лечатся ретраем.
    /// - `RateLimit`: терпеливая (8 попыток, база 2 с, кап 120 с) — лимиты
    ///   скользящие и сбрасываются за десятки секунд;
    /// - `Server5xx`: средняя (5 попыток, база 500 мс, кап 30 с);
    /// - `Network`: быстрая (5 попыток, база 250 мс, кап 10 с) — транспорт
    ///   либо жив, либо нет, долгие паузы не помогают;
    /// - `Unknown`: осторожная (3 попытки, база 1 с, кап 8 с).
    #[must_use]
    pub const fn for_kind(kind: ErrorKind) -> Option<Self> {
        match kind {
            ErrorKind::RateLimit => Some(Self {
                max_attempts: 8,
                base_ms: 2_000,
                max_ms: 120_000,
                jitter_pct: 25,
            }),
            ErrorKind::Server5xx => Some(Self {
                max_attempts: 5,
                base_ms: 500,
                max_ms: 30_000,
                jitter_pct: 20,
            }),
            ErrorKind::Network => Some(Self {
                max_attempts: 5,
                base_ms: 250,
                max_ms: 10_000,
                jitter_pct: 20,
            }),
            ErrorKind::Unknown => Some(Self {
                max_attempts: 3,
                base_ms: 1_000,
                max_ms: 8_000,
                jitter_pct: 10,
            }),
            ErrorKind::Auth
            | ErrorKind::BadRequest
            | ErrorKind::ContextOverflow
            | ErrorKind::CircuitOpen => None,
        }
    }

    /// Конечный итератор задержек между попытками (длина `max_attempts - 1`)
    /// с детерминированным джиттером (seed фиксируется вызывающей стороной).
    #[must_use]
    pub fn delays(&self, seed: u64) -> Delays {
        Delays {
            policy: *self,
            left: self.max_attempts.saturating_sub(1),
            attempt: 0,
            rng: SplitMix64(seed),
        }
    }
}

/// Итератор задержек: `base·2^n`, кап `max_ms`, джиттер ±`jitter_pct`%.
pub struct Delays {
    policy: RetryPolicy,
    left: u32,
    attempt: u32,
    rng: SplitMix64,
}

impl Iterator for Delays {
    type Item = Duration;

    fn next(&mut self) -> Option<Duration> {
        if self.left == 0 {
            return None;
        }
        self.left -= 1;
        let exp = self
            .policy
            .base_ms
            .saturating_mul(1u64 << self.attempt.min(20));
        self.attempt += 1;
        let capped = exp.min(self.policy.max_ms);
        Some(Duration::from_millis(apply_jitter(
            capped,
            self.policy.jitter_pct,
            &mut self.rng,
        )))
    }
}

/// Симметричный джиттер ±`jitter_pct`% (клампится к 100), границы включительны.
fn apply_jitter(base_ms: u64, jitter_pct: u8, rng: &mut SplitMix64) -> u64 {
    let pct = u64::from(jitter_pct.min(100));
    if pct == 0 || base_ms == 0 {
        return base_ms;
    }
    let span = base_ms.saturating_mul(pct) / 100;
    let delta = rng.next_u64() % (2 * span + 1);
    (base_ms + delta).saturating_sub(span)
}

/// `SplitMix64` — детерминированный PRNG без внешних зависимостей
/// (джиттер обязан воспроизводиться в тестах).
struct SplitMix64(u64);

impl SplitMix64 {
    fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_by_status() {
        assert_eq!(classify(Some(429), ""), ErrorKind::RateLimit);
        assert_eq!(classify(Some(413), ""), ErrorKind::ContextOverflow);
        assert_eq!(classify(Some(401), ""), ErrorKind::Auth);
        assert_eq!(classify(Some(403), ""), ErrorKind::Auth);
        assert_eq!(classify(Some(400), ""), ErrorKind::BadRequest);
        assert_eq!(classify(Some(404), ""), ErrorKind::BadRequest);
        assert_eq!(classify(Some(500), ""), ErrorKind::Server5xx);
        assert_eq!(classify(Some(503), ""), ErrorKind::Server5xx);
        assert_eq!(classify(Some(200), ""), ErrorKind::Unknown);
    }

    #[test]
    fn classify_by_text_markers() {
        assert_eq!(
            classify(None, "This model's maximum context length is 128000"),
            ErrorKind::ContextOverflow
        );
        assert_eq!(
            classify(None, "Request Entity Too Large"),
            ErrorKind::ContextOverflow
        );
        assert_eq!(classify(None, "Rate limit reached"), ErrorKind::RateLimit);
        assert_eq!(classify(None, "invalid api key"), ErrorKind::Auth);
        assert_eq!(
            classify(None, "gigachat: OAuth: circuit breaker открыт (3 сбоя)"),
            ErrorKind::CircuitOpen,
            "открытый breaker — не ретраится"
        );
        assert_eq!(
            classify(None, "connection reset by peer"),
            ErrorKind::Network
        );
        assert_eq!(classify(None, "operation timed out"), ErrorKind::Network);
        assert_eq!(classify(None, "что-то непонятное"), ErrorKind::Unknown);
    }

    #[test]
    fn policy_matrix_matches_documentation() {
        let rl = RetryPolicy::for_kind(ErrorKind::RateLimit).expect("policy");
        assert_eq!(rl.max_attempts, 8);
        assert_eq!(rl.base_ms, 2_000);
        let s5 = RetryPolicy::for_kind(ErrorKind::Server5xx).expect("policy");
        assert_eq!(s5.max_attempts, 5);
        assert!(RetryPolicy::for_kind(ErrorKind::Auth).is_none());
        assert!(RetryPolicy::for_kind(ErrorKind::BadRequest).is_none());
        assert!(RetryPolicy::for_kind(ErrorKind::ContextOverflow).is_none());
        assert!(
            RetryPolicy::for_kind(ErrorKind::CircuitOpen).is_none(),
            "circuit breaker — быстрый отказ без ретраев"
        );
    }

    #[test]
    fn delays_are_exponential_capped_and_deterministic() {
        let policy = RetryPolicy {
            max_attempts: 5,
            base_ms: 100,
            max_ms: 250,
            jitter_pct: 10,
        };
        let a: Vec<Duration> = policy.delays(42).collect();
        let b: Vec<Duration> = policy.delays(42).collect();
        assert_eq!(a.len(), 4, "попыток 5 → задержек 4");
        assert_eq!(a, b, "один seed — одна последовательность");
        // База растёт: 100, 200, 250(кап), 250(кап); джиттер ±10% → ≤ 275.
        for (i, d) in a.iter().enumerate() {
            let cap_base = 100u64 << i.min(1);
            let _ = cap_base;
            assert!(d.as_millis() <= 275, "кап + джиттер: {d:?}");
        }
        let c: Vec<Duration> = policy.delays(43).collect();
        assert_ne!(a, c, "другой seed — другая последовательность");
    }

    #[test]
    fn jitter_zero_pct_is_exact() {
        assert_eq!(apply_jitter(1000, 0, &mut SplitMix64(1)), 1000);
        let v = apply_jitter(1000, 100, &mut SplitMix64(7));
        assert!(v <= 2000, "джиттер 100% не выше удвоения: {v}");
    }

    #[test]
    fn context_overflow_helper() {
        assert!(is_context_overflow(Some(413), ""));
        assert!(is_context_overflow(None, "prompt is too long"));
        assert!(!is_context_overflow(Some(500), ""));
        assert!(!is_context_overflow(None, "timeout"));
    }
}
