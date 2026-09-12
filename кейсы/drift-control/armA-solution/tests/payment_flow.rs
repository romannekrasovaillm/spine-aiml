//! Integration tests exercising the public API end to end.

use payment_core::{Currency, Payment, PaymentError, PaymentStatus};

#[test]
fn happy_path_authorize_capture_refund() {
    let mut payment = Payment::new("inv_42", 2_500, Currency::EUR).unwrap();
    assert_eq!(payment.status(), PaymentStatus::Created);

    payment.authorize().expect("authorize should succeed");
    assert_eq!(payment.status(), PaymentStatus::Authorized);

    payment
        .capture(1_000)
        .expect("partial capture should succeed");
    assert_eq!(payment.status(), PaymentStatus::PartiallyCaptured);

    payment
        .capture(1_500)
        .expect("final capture should succeed");
    assert_eq!(payment.status(), PaymentStatus::Captured);

    payment.refund(2_500).expect("refund should succeed");
    assert_eq!(payment.status(), PaymentStatus::Refunded);

    // created + authorized + 2 captures + refund
    assert_eq!(payment.events().len(), 5);
}

#[test]
fn capture_before_authorize_is_rejected() {
    let mut payment = Payment::new("inv_43", 100, Currency::USD).unwrap();
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
fn partial_refund_flow() {
    let mut payment = Payment::new("inv_44", 1_000, Currency::USD).unwrap();
    payment.authorize().unwrap();
    payment.capture(1_000).unwrap();

    payment.refund(250).unwrap();
    assert_eq!(payment.status(), PaymentStatus::PartiallyRefunded);

    payment.refund(750).unwrap();
    assert_eq!(payment.status(), PaymentStatus::Refunded);
}

#[test]
fn voided_payment_is_terminal() {
    let mut payment = Payment::new("inv_45", 500, Currency::RUB).unwrap();
    payment.authorize().unwrap();
    payment.void().unwrap();
    assert_eq!(payment.status(), PaymentStatus::Voided);
    assert!(payment.status().is_terminal());
}
