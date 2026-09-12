//! Payment lifecycle statuses.

use std::fmt;

/// The lifecycle status of a [`crate::Payment`].
///
/// Transitions follow a well-defined state machine:
///
/// - `Created` → `Authorized` via [`authorize`](crate::Payment::authorize)
/// - `Authorized` → `PartiallyCaptured`/`Captured` via
///   [`capture`](crate::Payment::capture)
/// - `Captured`/`PartiallyCaptured` → `PartiallyRefunded`/`Refunded` via
///   [`refund`](crate::Payment::refund)
/// - `Authorized` → `Voided` via [`void`](crate::Payment::void)
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PaymentStatus {
    /// The payment has been created but not yet authorized.
    Created,
    /// Funds have been authorized but not yet captured.
    Authorized,
    /// A portion of the authorized amount has been captured.
    PartiallyCaptured,
    /// The full authorized amount has been captured.
    Captured,
    /// A portion of the captured amount has been refunded.
    PartiallyRefunded,
    /// The full captured amount has been refunded.
    Refunded,
    /// The authorization was voided; no funds were captured.
    Voided,
}

impl PaymentStatus {
    /// Returns `true` if no further transitions are possible from this status.
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Refunded | Self::Voided)
    }
}

impl fmt::Display for PaymentStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let name = match self {
            Self::Created => "created",
            Self::Authorized => "authorized",
            Self::PartiallyCaptured => "partially_captured",
            Self::Captured => "captured",
            Self::PartiallyRefunded => "partially_refunded",
            Self::Refunded => "refunded",
            Self::Voided => "voided",
        };
        f.write_str(name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_states() {
        assert!(PaymentStatus::Refunded.is_terminal());
        assert!(PaymentStatus::Voided.is_terminal());
        assert!(!PaymentStatus::Created.is_terminal());
        assert!(!PaymentStatus::Authorized.is_terminal());
        assert!(!PaymentStatus::Captured.is_terminal());
    }

    #[test]
    fn display_names() {
        assert_eq!(PaymentStatus::Created.to_string(), "created");
        assert_eq!(PaymentStatus::Authorized.to_string(), "authorized");
        assert_eq!(
            PaymentStatus::PartiallyCaptured.to_string(),
            "partially_captured"
        );
        assert_eq!(
            PaymentStatus::PartiallyRefunded.to_string(),
            "partially_refunded"
        );
        assert_eq!(PaymentStatus::Refunded.to_string(), "refunded");
        assert_eq!(PaymentStatus::Voided.to_string(), "voided");
    }
}
