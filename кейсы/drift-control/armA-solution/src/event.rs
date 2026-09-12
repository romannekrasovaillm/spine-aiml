//! The payment event journal.

use std::fmt;
use std::time::SystemTime;

use crate::status::PaymentStatus;

/// The kind of a [`PaymentEvent`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PaymentEventType {
    /// The payment was created.
    Created,
    /// The payment was authorized.
    Authorized,
    /// Funds were captured (fully or partially).
    Captured,
    /// Funds were refunded (fully or partially).
    Refunded,
    /// The authorization was voided.
    Voided,
}

impl fmt::Display for PaymentEventType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let name = match self {
            Self::Created => "created",
            Self::Authorized => "authorized",
            Self::Captured => "captured",
            Self::Refunded => "refunded",
            Self::Voided => "voided",
        };
        f.write_str(name)
    }
}

/// A single entry in a payment's append-only event journal.
///
/// Each entry records the type of event, the amount it applied to (when
/// relevant), the payment status immediately after the event, a human-readable
/// description, and the time the event occurred.
#[derive(Debug, Clone)]
pub struct PaymentEvent {
    event_type: PaymentEventType,
    amount: Option<u64>,
    status_after: PaymentStatus,
    description: String,
    occurred_at: SystemTime,
}

impl PaymentEvent {
    pub(crate) fn new(
        event_type: PaymentEventType,
        amount: Option<u64>,
        status_after: PaymentStatus,
        description: String,
    ) -> Self {
        Self {
            event_type,
            amount,
            status_after,
            description,
            occurred_at: SystemTime::now(),
        }
    }

    /// The type of this event.
    pub fn event_type(&self) -> PaymentEventType {
        self.event_type
    }

    /// The amount this event applied to, if any.
    pub fn amount(&self) -> Option<u64> {
        self.amount
    }

    /// The payment status immediately after this event occurred.
    pub fn status_after(&self) -> PaymentStatus {
        self.status_after
    }

    /// A human-readable description of the event.
    pub fn description(&self) -> &str {
        &self.description
    }

    /// The wall-clock time at which the event occurred.
    pub fn occurred_at(&self) -> SystemTime {
        self.occurred_at
    }
}

/// Equality ignores the `occurred_at` timestamp so that journal contents can be
/// compared deterministically in tests.
impl PartialEq for PaymentEvent {
    fn eq(&self, other: &Self) -> bool {
        self.event_type == other.event_type
            && self.amount == other.amount
            && self.status_after == other.status_after
            && self.description == other.description
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accessors_expose_fields() {
        let event = PaymentEvent::new(
            PaymentEventType::Captured,
            Some(100),
            PaymentStatus::PartiallyCaptured,
            "captured 100".to_string(),
        );

        assert_eq!(event.event_type(), PaymentEventType::Captured);
        assert_eq!(event.amount(), Some(100));
        assert_eq!(event.status_after(), PaymentStatus::PartiallyCaptured);
        assert_eq!(event.description(), "captured 100");
    }

    #[test]
    fn equality_ignores_timestamp() {
        let a = PaymentEvent::new(
            PaymentEventType::Authorized,
            None,
            PaymentStatus::Authorized,
            "authorized".to_string(),
        );
        let b = PaymentEvent::new(
            PaymentEventType::Authorized,
            None,
            PaymentStatus::Authorized,
            "authorized".to_string(),
        );
        assert_eq!(a, b);
    }
}
