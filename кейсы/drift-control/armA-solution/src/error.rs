//! Error types for the payment core.

use std::fmt;

use crate::status::PaymentStatus;

/// Convenience alias for fallible payment operations.
pub type PaymentResult<T> = Result<T, PaymentError>;

/// Errors that can occur while working with [`crate::Payment`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaymentError {
    /// An operation was attempted that is not legal in the payment's current
    /// [`PaymentStatus`].
    InvalidTransition {
        /// The operation that was attempted, e.g. `"capture"`.
        operation: &'static str,
        /// The status the payment was in when the operation was attempted.
        current: PaymentStatus,
    },
    /// An amount of zero was supplied where a strictly positive amount is
    /// required.
    ZeroAmount,
    /// A capture amount exceeds the amount still authorized for capture.
    AmountExceedsAuthorized {
        /// The amount that was requested.
        amount: u64,
        /// The amount still available to capture.
        remaining: u64,
    },
    /// A refund amount exceeds the amount captured and not yet refunded.
    AmountExceedsCaptured {
        /// The amount that was requested.
        amount: u64,
        /// The amount still available to refund.
        available: u64,
    },
    /// A currency code could not be resolved to a known ISO-4217 code.
    UnknownCurrency(String),
    /// A payment id was empty or consisted only of whitespace.
    InvalidPaymentId,
}

impl PaymentError {
    pub(crate) fn invalid_transition(operation: &'static str, current: PaymentStatus) -> Self {
        Self::InvalidTransition { operation, current }
    }
}

impl fmt::Display for PaymentError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidTransition { operation, current } => {
                write!(
                    f,
                    "operation '{operation}' is not allowed from status '{current}'"
                )
            }
            Self::ZeroAmount => write!(f, "amount must be greater than zero"),
            Self::AmountExceedsAuthorized { amount, remaining } => {
                write!(
                    f,
                    "capture amount {amount} exceeds remaining authorized amount {remaining}"
                )
            }
            Self::AmountExceedsCaptured { amount, available } => {
                write!(
                    f,
                    "refund amount {amount} exceeds captured amount available for refund {available}"
                )
            }
            Self::UnknownCurrency(code) => write!(f, "unknown ISO-4217 currency code '{code}'"),
            Self::InvalidPaymentId => write!(f, "payment id must not be empty"),
        }
    }
}

impl std::error::Error for PaymentError {}
