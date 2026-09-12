//! `payments_core` — a minimal payment core library.
//!
//! # Money
//!
//! All monetary amounts are expressed in **minor units** (`i64`): kopecks for
//! RUB, cents for USD/EUR. Floating-point arithmetic is never used for money
//! anywhere in this crate; callers format to "rubles.kopecks" only at the
//! presentation boundary.
//!
//! # Errors
//!
//! Every failure is a variant of the [`PaymentError`] enum (built with
//! `thiserror`), so callers can exhaustively `match` on the failure kind.
//!
//! # Idempotency
//!
//! [`PaymentProcessor::authorize`] takes an idempotency key. Repeating a call
//! with the same key returns the first result (success or failure) without
//! repeating the side effect, which prevents double authorization on client
//! retries.

use std::collections::HashMap;

use thiserror::Error;

/// Currency of a payment. The associated amount is always in minor units.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Currency {
    /// Russian ruble; amount in kopecks.
    Rub,
    /// US dollar; amount in cents.
    Usd,
    /// Euro; amount in cents.
    Eur,
}

/// Lifecycle status of a payment.
///
/// Transitions:
///
/// ```text
/// Pending -> Authorized   (authorize)
/// Authorized -> Captured / PartiallyCaptured   (capture)
/// Captured -> PartiallyRefunded -> Refunded    (refund)
/// Authorized -> Voided                         (void)
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaymentStatus {
    /// Created but not yet authorized.
    Pending,
    /// Funds are held (authorized) but nothing captured.
    Authorized,
    /// Fully captured, nothing refunded.
    Captured,
    /// Partially captured; some authorized amount is still uncaptured.
    PartiallyCaptured,
    /// Fully refunded.
    Refunded,
    /// Partially refunded; some captured amount is still unrefunded.
    PartiallyRefunded,
    /// Authorization released before any capture.
    Voided,
}

/// Errors produced by the payment core.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum PaymentError {
    #[error("amount must be strictly positive")]
    NonPositiveAmount,

    #[error("currency mismatch: expected {expected:?}, got {actual:?}")]
    CurrencyMismatch {
        expected: Currency,
        actual: Currency,
    },

    #[error("invalid state transition: cannot {action} from {from:?}")]
    InvalidTransition {
        from: PaymentStatus,
        action: &'static str,
    },

    #[error("capture amount {requested} exceeds authorized {authorized}")]
    CaptureExceedsAuthorized { requested: i64, authorized: i64 },

    #[error("refund amount {requested} exceeds captured {captured}")]
    RefundExceedsCaptured { requested: i64, captured: i64 },
}

/// An immutable event appended to the payment journal on every transition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaymentEvent {
    /// Payment created.
    Created { amount: i64, currency: Currency },
    /// Authorization succeeded (funds held).
    Authorized { idempotency_key: String },
    /// Some amount was captured.
    Captured { amount: i64 },
    /// Some amount was refunded.
    Refunded { amount: i64 },
    /// Authorization was voided.
    Voided,
}

/// A payment carrying its amount, currency, status and an event journal.
///
/// `Payment` is a value type: every state-changing method consumes `self` and
/// returns a new `Payment` (or an error), so an invalid transition can never
/// leave a half-mutated payment behind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Payment {
    amount: i64,
    currency: Currency,
    status: PaymentStatus,
    captured: i64,
    refunded: i64,
    events: Vec<PaymentEvent>,
}

impl Payment {
    /// Creates a new `Pending` payment for `amount` minor units of `currency`.
    pub fn new(amount: i64, currency: Currency) -> Result<Self, PaymentError> {
        if amount <= 0 {
            return Err(PaymentError::NonPositiveAmount);
        }
        Ok(Self {
            amount,
            currency,
            status: PaymentStatus::Pending,
            captured: 0,
            refunded: 0,
            events: vec![PaymentEvent::Created { amount, currency }],
        })
    }

    /// Authorized (held) amount, in minor units.
    pub fn amount(&self) -> i64 {
        self.amount
    }

    pub fn currency(&self) -> Currency {
        self.currency
    }

    pub fn status(&self) -> PaymentStatus {
        self.status
    }

    /// Total captured amount, in minor units.
    pub fn captured_amount(&self) -> i64 {
        self.captured
    }

    /// Total refunded amount, in minor units.
    pub fn refunded_amount(&self) -> i64 {
        self.refunded
    }

    /// The append-only journal of events for this payment, in order.
    pub fn events(&self) -> &[PaymentEvent] {
        &self.events
    }

    /// Holds the funds: `Pending` -> `Authorized`.
    ///
    /// This is a bare transition without idempotency handling; use
    /// [`PaymentProcessor::authorize`] to deduplicate client retries by key.
    fn authorize(mut self, idempotency_key: &str) -> Result<Self, PaymentError> {
        if self.status != PaymentStatus::Pending {
            return Err(PaymentError::InvalidTransition {
                from: self.status,
                action: "authorize",
            });
        }
        self.status = PaymentStatus::Authorized;
        self.events.push(PaymentEvent::Authorized {
            idempotency_key: idempotency_key.to_owned(),
        });
        Ok(self)
    }

    /// Captures `amount` minor units of the authorized funds.
    ///
    /// Allowed from `Authorized` or `PartiallyCaptured`; the total captured
    /// amount may never exceed the authorized amount.
    pub fn capture(mut self, amount: i64) -> Result<Self, PaymentError> {
        if amount <= 0 {
            return Err(PaymentError::NonPositiveAmount);
        }
        match self.status {
            PaymentStatus::Authorized | PaymentStatus::PartiallyCaptured => {}
            other => {
                return Err(PaymentError::InvalidTransition {
                    from: other,
                    action: "capture",
                })
            }
        }
        let available = self.amount - self.captured;
        if amount > available {
            return Err(PaymentError::CaptureExceedsAuthorized {
                requested: amount,
                authorized: self.amount,
            });
        }
        self.captured += amount;
        self.status = if self.captured == self.amount {
            PaymentStatus::Captured
        } else {
            PaymentStatus::PartiallyCaptured
        };
        self.events.push(PaymentEvent::Captured { amount });
        Ok(self)
    }

    /// Refunds `amount` minor units of the captured funds.
    ///
    /// Allowed from `Captured` or `PartiallyRefunded` (i.e. only once the
    /// payment is fully captured); the total refunded amount may never exceed
    /// the captured amount.
    pub fn refund(mut self, amount: i64) -> Result<Self, PaymentError> {
        if amount <= 0 {
            return Err(PaymentError::NonPositiveAmount);
        }
        match self.status {
            PaymentStatus::Captured | PaymentStatus::PartiallyRefunded => {}
            other => {
                return Err(PaymentError::InvalidTransition {
                    from: other,
                    action: "refund",
                })
            }
        }
        let available = self.captured - self.refunded;
        if amount > available {
            return Err(PaymentError::RefundExceedsCaptured {
                requested: amount,
                captured: self.captured,
            });
        }
        self.refunded += amount;
        self.status = if self.refunded == self.captured {
            PaymentStatus::Refunded
        } else {
            PaymentStatus::PartiallyRefunded
        };
        self.events.push(PaymentEvent::Refunded { amount });
        Ok(self)
    }

    /// Releases the held funds: `Authorized` -> `Voided`.
    pub fn void(mut self) -> Result<Self, PaymentError> {
        if self.status != PaymentStatus::Authorized {
            return Err(PaymentError::InvalidTransition {
                from: self.status,
                action: "void",
            });
        }
        self.status = PaymentStatus::Voided;
        self.events.push(PaymentEvent::Voided);
        Ok(self)
    }
}

/// The outcome of a previously handled idempotency key.
#[derive(Debug, Clone)]
enum StoredAuthorization {
    Success(Payment),
    Failure(PaymentError),
}

/// Drives payments through their lifecycle, providing idempotent authorization.
#[derive(Debug, Default)]
pub struct PaymentProcessor {
    inbox: HashMap<String, StoredAuthorization>,
}

impl PaymentProcessor {
    /// Creates an empty processor with an empty idempotency inbox.
    pub fn new() -> Self {
        Self::default()
    }

    /// Authorizes `payment`, deduplicating retries by `idempotency_key`.
    ///
    /// The first call with a given key performs the authorization (recording
    /// the result). Any later call with the same key returns that first result
    /// verbatim — success returns a clone of the originally authorized payment,
    /// failure returns the original error — without touching the payment or the
    /// state machine again.
    pub fn authorize(
        &mut self,
        payment: Payment,
        idempotency_key: &str,
    ) -> Result<Payment, PaymentError> {
        if let Some(stored) = self.inbox.get(idempotency_key) {
            return match stored {
                StoredAuthorization::Success(payment) => Ok(payment.clone()),
                StoredAuthorization::Failure(error) => Err(error.clone()),
            };
        }

        let outcome = payment.authorize(idempotency_key);
        let stored = match &outcome {
            Ok(payment) => StoredAuthorization::Success(payment.clone()),
            Err(error) => StoredAuthorization::Failure(error.clone()),
        };
        self.inbox.insert(idempotency_key.to_owned(), stored);
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rub(amount: i64) -> Payment {
        Payment::new(amount, Currency::Rub).expect("valid amount")
    }

    fn authorized(processor: &mut PaymentProcessor, key: &str, amount: i64) -> Payment {
        processor
            .authorize(rub(amount), key)
            .expect("authorization should succeed")
    }

    #[test]
    fn new_payment_is_pending_with_created_event() {
        let payment = rub(1000);
        assert_eq!(payment.status(), PaymentStatus::Pending);
        assert_eq!(payment.amount(), 1000);
        assert_eq!(payment.currency(), Currency::Rub);
        assert_eq!(payment.captured_amount(), 0);
        assert_eq!(payment.refunded_amount(), 0);
        assert_eq!(
            payment.events(),
            &[PaymentEvent::Created {
                amount: 1000,
                currency: Currency::Rub,
            }][..]
        );
    }

    #[test]
    fn new_rejects_non_positive_amount() {
        assert_eq!(
            Payment::new(0, Currency::Rub).unwrap_err(),
            PaymentError::NonPositiveAmount
        );
        assert_eq!(
            Payment::new(-5, Currency::Usd).unwrap_err(),
            PaymentError::NonPositiveAmount
        );
    }

    #[test]
    fn authorize_transitions_pending_to_authorized() {
        let mut processor = PaymentProcessor::new();
        let payment = processor.authorize(rub(1000), "k1").unwrap();
        assert_eq!(payment.status(), PaymentStatus::Authorized);
        assert_eq!(payment.events().len(), 2);
        assert!(matches!(
            payment.events().last(),
            Some(PaymentEvent::Authorized { idempotency_key }) if idempotency_key == "k1"
        ));
    }

    #[test]
    fn authorize_is_idempotent_by_key() {
        let mut processor = PaymentProcessor::new();
        let first = processor.authorize(rub(1000), "k1").unwrap();

        // Same key, different payment: the first result is returned verbatim,
        // and no second authorization is performed.
        let second = processor
            .authorize(Payment::new(2000, Currency::Usd).unwrap(), "k1")
            .unwrap();

        assert_eq!(first, second);
        assert_eq!(second.amount(), 1000);
        assert_eq!(second.currency(), Currency::Rub);
        assert_eq!(second.events().len(), 2);
        assert_eq!(
            second
                .events()
                .iter()
                .filter(|event| matches!(event, PaymentEvent::Authorized { .. }))
                .count(),
            1
        );
    }

    #[test]
    fn authorize_different_keys_are_independent() {
        let mut processor = PaymentProcessor::new();
        let a = processor.authorize(rub(1000), "a").unwrap();
        let b = processor.authorize(rub(2000), "b").unwrap();
        assert_ne!(a, b);
        assert_eq!(a.amount(), 1000);
        assert_eq!(b.amount(), 2000);
    }

    #[test]
    fn authorize_failure_is_also_remembered() {
        let mut processor = PaymentProcessor::new();

        // Re-authorizing an already authorized payment under a fresh key fails.
        let already = authorized(&mut processor, "k1", 1000);
        let first = processor.authorize(already, "k2").unwrap_err();
        assert_eq!(
            first,
            PaymentError::InvalidTransition {
                from: PaymentStatus::Authorized,
                action: "authorize",
            }
        );

        // A retry with the same failing key returns the original failure, not a
        // fresh success.
        let retry = processor.authorize(rub(500), "k2").unwrap_err();
        assert_eq!(first, retry);
    }

    #[test]
    fn capture_full_authorized() {
        let mut processor = PaymentProcessor::new();
        let payment = authorized(&mut processor, "k", 1000).capture(1000).unwrap();
        assert_eq!(payment.status(), PaymentStatus::Captured);
        assert_eq!(payment.captured_amount(), 1000);
        assert!(matches!(
            payment.events().last(),
            Some(PaymentEvent::Captured { amount: 1000 })
        ));
    }

    #[test]
    fn capture_partial_then_remaining() {
        let mut processor = PaymentProcessor::new();
        let payment = authorized(&mut processor, "k", 1000).capture(400).unwrap();
        assert_eq!(payment.status(), PaymentStatus::PartiallyCaptured);
        assert_eq!(payment.captured_amount(), 400);

        let payment = payment.capture(600).unwrap();
        assert_eq!(payment.status(), PaymentStatus::Captured);
        assert_eq!(payment.captured_amount(), 1000);
    }

    #[test]
    fn capture_rejects_excess() {
        let mut processor = PaymentProcessor::new();
        let error = authorized(&mut processor, "k", 1000)
            .capture(1001)
            .unwrap_err();
        assert_eq!(
            error,
            PaymentError::CaptureExceedsAuthorized {
                requested: 1001,
                authorized: 1000,
            }
        );
    }

    #[test]
    fn capture_rejects_non_positive() {
        let mut processor = PaymentProcessor::new();
        let error = authorized(&mut processor, "k", 1000).capture(0).unwrap_err();
        assert_eq!(error, PaymentError::NonPositiveAmount);
    }

    #[test]
    fn capture_rejects_wrong_state() {
        let error = rub(1000).capture(1000).unwrap_err();
        assert_eq!(
            error,
            PaymentError::InvalidTransition {
                from: PaymentStatus::Pending,
                action: "capture",
            }
        );
    }

    #[test]
    fn refund_full() {
        let mut processor = PaymentProcessor::new();
        let payment = authorized(&mut processor, "k", 1000)
            .capture(1000)
            .unwrap()
            .refund(1000)
            .unwrap();
        assert_eq!(payment.status(), PaymentStatus::Refunded);
        assert_eq!(payment.refunded_amount(), 1000);
        assert!(matches!(
            payment.events().last(),
            Some(PaymentEvent::Refunded { amount: 1000 })
        ));
    }

    #[test]
    fn refund_partial_then_remaining() {
        let mut processor = PaymentProcessor::new();
        let payment = authorized(&mut processor, "k", 1000)
            .capture(1000)
            .unwrap()
            .refund(300)
            .unwrap();
        assert_eq!(payment.status(), PaymentStatus::PartiallyRefunded);
        assert_eq!(payment.refunded_amount(), 300);

        let payment = payment.refund(700).unwrap();
        assert_eq!(payment.status(), PaymentStatus::Refunded);
        assert_eq!(payment.refunded_amount(), 1000);
    }

    #[test]
    fn refund_rejects_excess() {
        let mut processor = PaymentProcessor::new();
        let error = authorized(&mut processor, "k", 1000)
            .capture(1000)
            .unwrap()
            .refund(1001)
            .unwrap_err();
        assert_eq!(
            error,
            PaymentError::RefundExceedsCaptured {
                requested: 1001,
                captured: 1000,
            }
        );
    }

    #[test]
    fn refund_rejects_before_capture() {
        let mut processor = PaymentProcessor::new();
        let error = authorized(&mut processor, "k", 1000).refund(100).unwrap_err();
        assert_eq!(
            error,
            PaymentError::InvalidTransition {
                from: PaymentStatus::Authorized,
                action: "refund",
            }
        );
    }

    #[test]
    fn void_releases_authorization() {
        let mut processor = PaymentProcessor::new();
        let payment = authorized(&mut processor, "k", 1000).void().unwrap();
        assert_eq!(payment.status(), PaymentStatus::Voided);
        assert!(matches!(payment.events().last(), Some(PaymentEvent::Voided)));
    }

    #[test]
    fn void_rejects_after_capture() {
        let mut processor = PaymentProcessor::new();
        let error = authorized(&mut processor, "k", 1000)
            .capture(1000)
            .unwrap()
            .void()
            .unwrap_err();
        assert_eq!(
            error,
            PaymentError::InvalidTransition {
                from: PaymentStatus::Captured,
                action: "void",
            }
        );
    }

    #[test]
    fn full_lifecycle_event_journal() {
        let mut processor = PaymentProcessor::new();
        let payment = authorized(&mut processor, "order-1", 1000)
            .capture(1000)
            .unwrap()
            .refund(1000)
            .unwrap();

        assert_eq!(
            payment.events(),
            &[
                PaymentEvent::Created {
                    amount: 1000,
                    currency: Currency::Rub,
                },
                PaymentEvent::Authorized {
                    idempotency_key: "order-1".to_string(),
                },
                PaymentEvent::Captured { amount: 1000 },
                PaymentEvent::Refunded { amount: 1000 },
            ][..]
        );
    }

    #[test]
    fn amounts_are_tracked_in_minor_units() {
        // 1234 minor units == 12.34 RUB — no floating point involved.
        let mut processor = PaymentProcessor::new();
        let payment = authorized(&mut processor, "k", 1234);
        assert_eq!(payment.amount(), 1234);
        let payment = payment.capture(1234).unwrap();
        assert_eq!(payment.captured_amount(), 1234);
    }
}
