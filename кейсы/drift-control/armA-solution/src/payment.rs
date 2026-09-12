//! The [`Payment`] entity and its lifecycle operations.

use crate::currency::Currency;
use crate::error::{PaymentError, PaymentResult};
use crate::event::{PaymentEvent, PaymentEventType};
use crate::status::PaymentStatus;

/// An amount of money expressed in the smallest currency unit (minor units),
/// e.g. cents for `USD` or kopecks for `RUB`.
///
/// Stored as an unsigned integer to avoid floating point rounding errors.
pub type Amount = u64;

/// A payment in the payment core.
///
/// A [`Payment`] holds the original amount and currency, its current lifecycle
/// [`PaymentStatus`], how much of the amount has been captured and refunded so
/// far, and an append-only journal of [`PaymentEvent`]s.
///
/// # Lifecycle
///
/// - `Created` → `Authorized` via [`authorize`](Payment::authorize)
/// - `Authorized` → `PartiallyCaptured`/`Captured` via [`capture`](Payment::capture)
/// - `Captured`/`PartiallyCaptured` → `PartiallyRefunded`/`Refunded` via
///   [`refund`](Payment::refund)
/// - `Authorized` → `Voided` via [`void`](Payment::void)
///
/// Capture and refund support partial amounts; the resulting status reflects
/// whether the full amount was consumed.
#[derive(Debug, Clone, PartialEq)]
pub struct Payment {
    id: String,
    amount: Amount,
    currency: Currency,
    status: PaymentStatus,
    captured_amount: Amount,
    refunded_amount: Amount,
    events: Vec<PaymentEvent>,
}

impl Payment {
    /// Creates a new payment in the `Created` status.
    ///
    /// # Errors
    ///
    /// Returns [`PaymentError::InvalidPaymentId`] if `id` is empty or
    /// whitespace, and [`PaymentError::ZeroAmount`] if `amount` is zero.
    pub fn new(id: impl Into<String>, amount: Amount, currency: Currency) -> PaymentResult<Self> {
        let id = id.into();
        if id.trim().is_empty() {
            return Err(PaymentError::InvalidPaymentId);
        }
        if amount == 0 {
            return Err(PaymentError::ZeroAmount);
        }

        let event = PaymentEvent::new(
            PaymentEventType::Created,
            None,
            PaymentStatus::Created,
            format!("payment '{id}' created for {amount} {currency}"),
        );

        Ok(Self {
            id,
            amount,
            currency,
            status: PaymentStatus::Created,
            captured_amount: 0,
            refunded_amount: 0,
            events: vec![event],
        })
    }

    /// Authorizes the payment, transitioning `Created` → `Authorized`.
    ///
    /// # Errors
    ///
    /// Returns [`PaymentError::InvalidTransition`] unless the payment is in the
    /// `Created` status.
    pub fn authorize(&mut self) -> PaymentResult<&PaymentEvent> {
        if self.status != PaymentStatus::Created {
            return Err(PaymentError::invalid_transition("authorize", self.status));
        }

        self.status = PaymentStatus::Authorized;
        let description = format!("payment '{}' authorized", self.id);
        Ok(self.record(PaymentEventType::Authorized, None, description))
    }

    /// Captures `amount` from the authorized amount.
    ///
    /// A capture may cover the full authorized amount (transitioning to
    /// `Captured`) or a portion of it (transitioning to `PartiallyCaptured`).
    ///
    /// # Errors
    ///
    /// - [`PaymentError::InvalidTransition`] if the payment is not
    ///   `Authorized` or `PartiallyCaptured`.
    /// - [`PaymentError::ZeroAmount`] if `amount` is zero.
    /// - [`PaymentError::AmountExceedsAuthorized`] if `amount` exceeds the
    ///   amount still available to capture.
    pub fn capture(&mut self, amount: Amount) -> PaymentResult<&PaymentEvent> {
        let current = self.status;
        if !matches!(
            current,
            PaymentStatus::Authorized | PaymentStatus::PartiallyCaptured
        ) {
            return Err(PaymentError::invalid_transition("capture", current));
        }
        if amount == 0 {
            return Err(PaymentError::ZeroAmount);
        }

        let remaining = self.amount - self.captured_amount;
        if amount > remaining {
            return Err(PaymentError::AmountExceedsAuthorized { amount, remaining });
        }

        self.captured_amount += amount;
        self.status = if self.captured_amount == self.amount {
            PaymentStatus::Captured
        } else {
            PaymentStatus::PartiallyCaptured
        };

        let description = format!("captured {amount} {}", self.currency);
        Ok(self.record(PaymentEventType::Captured, Some(amount), description))
    }

    /// Refunds `amount` from the captured amount.
    ///
    /// A refund may cover the full captured amount (transitioning to
    /// `Refunded`) or a portion of it (transitioning to `PartiallyRefunded`).
    ///
    /// # Errors
    ///
    /// - [`PaymentError::InvalidTransition`] if the payment is not `Captured`,
    ///   `PartiallyCaptured` or `PartiallyRefunded`.
    /// - [`PaymentError::ZeroAmount`] if `amount` is zero.
    /// - [`PaymentError::AmountExceedsCaptured`] if `amount` exceeds the
    ///   captured amount still available to refund.
    pub fn refund(&mut self, amount: Amount) -> PaymentResult<&PaymentEvent> {
        let current = self.status;
        if !matches!(
            current,
            PaymentStatus::Captured
                | PaymentStatus::PartiallyCaptured
                | PaymentStatus::PartiallyRefunded
        ) {
            return Err(PaymentError::invalid_transition("refund", current));
        }
        if amount == 0 {
            return Err(PaymentError::ZeroAmount);
        }

        let available = self.captured_amount - self.refunded_amount;
        if amount > available {
            return Err(PaymentError::AmountExceedsCaptured { amount, available });
        }

        self.refunded_amount += amount;
        self.status = if self.refunded_amount == self.captured_amount {
            PaymentStatus::Refunded
        } else {
            PaymentStatus::PartiallyRefunded
        };

        let description = format!("refunded {amount} {}", self.currency);
        Ok(self.record(PaymentEventType::Refunded, Some(amount), description))
    }

    /// Voids an authorization, transitioning `Authorized` → `Voided`.
    ///
    /// # Errors
    ///
    /// Returns [`PaymentError::InvalidTransition`] unless the payment is in the
    /// `Authorized` status.
    pub fn void(&mut self) -> PaymentResult<&PaymentEvent> {
        if self.status != PaymentStatus::Authorized {
            return Err(PaymentError::invalid_transition("void", self.status));
        }

        self.status = PaymentStatus::Voided;
        let description = format!("payment '{}' voided", self.id);
        Ok(self.record(PaymentEventType::Voided, None, description))
    }

    /// Returns the payment id.
    pub fn id(&self) -> &str {
        &self.id
    }

    /// Returns the original authorized amount.
    pub fn amount(&self) -> Amount {
        self.amount
    }

    /// Returns the currency.
    pub fn currency(&self) -> Currency {
        self.currency
    }

    /// Returns the current lifecycle status.
    pub fn status(&self) -> PaymentStatus {
        self.status
    }

    /// Returns the total amount captured so far.
    pub fn captured_amount(&self) -> Amount {
        self.captured_amount
    }

    /// Returns the total amount refunded so far.
    pub fn refunded_amount(&self) -> Amount {
        self.refunded_amount
    }

    /// Returns the amount still available to capture.
    pub fn available_for_capture(&self) -> Amount {
        self.amount - self.captured_amount
    }

    /// Returns the captured amount still available to refund.
    pub fn available_for_refund(&self) -> Amount {
        self.captured_amount - self.refunded_amount
    }

    /// Returns the append-only event journal, in chronological order.
    pub fn events(&self) -> &[PaymentEvent] {
        &self.events
    }

    /// Returns the most recent journal entry, if any.
    pub fn last_event(&self) -> Option<&PaymentEvent> {
        self.events.last()
    }

    fn record(
        &mut self,
        event_type: PaymentEventType,
        amount: Option<Amount>,
        description: String,
    ) -> &PaymentEvent {
        let event = PaymentEvent::new(event_type, amount, self.status, description);
        self.events.push(event);
        self.events.last().expect("event was just pushed")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::PaymentError;

    fn new_payment() -> Payment {
        Payment::new("pay_test", 1_000, Currency::USD).unwrap()
    }

    #[test]
    fn new_payment_starts_created_with_event() {
        let payment = new_payment();
        assert_eq!(payment.id(), "pay_test");
        assert_eq!(payment.status(), PaymentStatus::Created);
        assert_eq!(payment.amount(), 1_000);
        assert_eq!(payment.currency(), Currency::USD);
        assert_eq!(payment.captured_amount(), 0);
        assert_eq!(payment.refunded_amount(), 0);
        assert_eq!(payment.events().len(), 1);
        assert_eq!(payment.events()[0].event_type(), PaymentEventType::Created);
        assert_eq!(payment.events()[0].status_after(), PaymentStatus::Created);
    }

    #[test]
    fn rejects_empty_id() {
        assert_eq!(
            Payment::new("", 100, Currency::USD),
            Err(PaymentError::InvalidPaymentId)
        );
        assert_eq!(
            Payment::new("   ", 100, Currency::USD),
            Err(PaymentError::InvalidPaymentId)
        );
    }

    #[test]
    fn rejects_zero_amount() {
        assert_eq!(
            Payment::new("p", 0, Currency::USD),
            Err(PaymentError::ZeroAmount)
        );
    }

    #[test]
    fn authorize_transitions_and_records_event() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        assert_eq!(payment.status(), PaymentStatus::Authorized);
        assert_eq!(payment.events().len(), 2);
        assert_eq!(
            payment.events()[1].event_type(),
            PaymentEventType::Authorized
        );
    }

    #[test]
    fn authorize_twice_fails() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        let err = payment.authorize().unwrap_err();
        assert_eq!(
            err,
            PaymentError::InvalidTransition {
                operation: "authorize",
                current: PaymentStatus::Authorized,
            }
        );
    }

    #[test]
    fn capture_full_amount() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        payment.capture(1_000).unwrap();
        assert_eq!(payment.status(), PaymentStatus::Captured);
        assert_eq!(payment.captured_amount(), 1_000);
    }

    #[test]
    fn partial_capture_then_full() {
        let mut payment = new_payment();
        payment.authorize().unwrap();

        payment.capture(400).unwrap();
        assert_eq!(payment.status(), PaymentStatus::PartiallyCaptured);
        assert_eq!(payment.captured_amount(), 400);
        assert_eq!(payment.available_for_capture(), 600);

        payment.capture(600).unwrap();
        assert_eq!(payment.status(), PaymentStatus::Captured);
        assert_eq!(payment.available_for_capture(), 0);
    }

    #[test]
    fn capture_before_authorize_fails() {
        let mut payment = new_payment();
        let err = payment.capture(100).unwrap_err();
        assert_eq!(
            err,
            PaymentError::InvalidTransition {
                operation: "capture",
                current: PaymentStatus::Created,
            }
        );
    }

    #[test]
    fn capture_over_amount_fails() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        let err = payment.capture(1_001).unwrap_err();
        assert_eq!(
            err,
            PaymentError::AmountExceedsAuthorized {
                amount: 1_001,
                remaining: 1_000,
            }
        );
    }

    #[test]
    fn capture_zero_fails() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        assert_eq!(payment.capture(0).unwrap_err(), PaymentError::ZeroAmount);
    }

    #[test]
    fn refund_full_after_capture() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        payment.capture(1_000).unwrap();
        payment.refund(1_000).unwrap();
        assert_eq!(payment.status(), PaymentStatus::Refunded);
        assert_eq!(payment.refunded_amount(), 1_000);
        assert!(payment.status().is_terminal());
    }

    #[test]
    fn partial_refund_then_full() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        payment.capture(1_000).unwrap();

        payment.refund(300).unwrap();
        assert_eq!(payment.status(), PaymentStatus::PartiallyRefunded);
        assert_eq!(payment.refunded_amount(), 300);
        assert_eq!(payment.available_for_refund(), 700);

        payment.refund(700).unwrap();
        assert_eq!(payment.status(), PaymentStatus::Refunded);
    }

    #[test]
    fn refund_before_capture_fails() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        let err = payment.refund(100).unwrap_err();
        assert_eq!(
            err,
            PaymentError::InvalidTransition {
                operation: "refund",
                current: PaymentStatus::Authorized,
            }
        );
    }

    #[test]
    fn refund_over_captured_fails() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        payment.capture(500).unwrap();
        let err = payment.refund(600).unwrap_err();
        assert_eq!(
            err,
            PaymentError::AmountExceedsCaptured {
                amount: 600,
                available: 500,
            }
        );
    }

    #[test]
    fn void_from_authorized() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        payment.void().unwrap();
        assert_eq!(payment.status(), PaymentStatus::Voided);
        assert!(payment.status().is_terminal());
    }

    #[test]
    fn void_from_created_fails() {
        let mut payment = new_payment();
        let err = payment.void().unwrap_err();
        assert_eq!(
            err,
            PaymentError::InvalidTransition {
                operation: "void",
                current: PaymentStatus::Created,
            }
        );
    }

    #[test]
    fn terminal_refunded_rejects_further_operations() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        payment.capture(1_000).unwrap();
        payment.refund(1_000).unwrap();

        assert!(matches!(
            payment.authorize().unwrap_err(),
            PaymentError::InvalidTransition { .. }
        ));
        assert!(matches!(
            payment.capture(1).unwrap_err(),
            PaymentError::InvalidTransition { .. }
        ));
        assert!(matches!(
            payment.refund(1).unwrap_err(),
            PaymentError::InvalidTransition { .. }
        ));
    }

    #[test]
    fn full_lifecycle_journal_sequence() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        payment.capture(600).unwrap();
        payment.capture(400).unwrap();
        payment.refund(1_000).unwrap();

        let events = payment.events();
        assert_eq!(events.len(), 5);

        assert_eq!(events[0].event_type(), PaymentEventType::Created);
        assert_eq!(events[0].status_after(), PaymentStatus::Created);

        assert_eq!(events[1].event_type(), PaymentEventType::Authorized);
        assert_eq!(events[1].status_after(), PaymentStatus::Authorized);

        assert_eq!(events[2].event_type(), PaymentEventType::Captured);
        assert_eq!(events[2].amount(), Some(600));
        assert_eq!(events[2].status_after(), PaymentStatus::PartiallyCaptured);

        assert_eq!(events[3].event_type(), PaymentEventType::Captured);
        assert_eq!(events[3].amount(), Some(400));
        assert_eq!(events[3].status_after(), PaymentStatus::Captured);

        assert_eq!(events[4].event_type(), PaymentEventType::Refunded);
        assert_eq!(events[4].amount(), Some(1_000));
        assert_eq!(events[4].status_after(), PaymentStatus::Refunded);

        assert_eq!(payment.last_event(), Some(&events[4]));
    }

    #[test]
    fn failed_operation_does_not_mutate_state_or_journal() {
        let mut payment = new_payment();
        payment.authorize().unwrap();
        payment.capture(400).unwrap();

        let events_before = payment.events().len();

        assert!(payment.capture(700).is_err()); // exceeds remaining 600

        assert_eq!(payment.events().len(), events_before);
        assert_eq!(payment.status(), PaymentStatus::PartiallyCaptured);
        assert_eq!(payment.captured_amount(), 400);
    }
}
