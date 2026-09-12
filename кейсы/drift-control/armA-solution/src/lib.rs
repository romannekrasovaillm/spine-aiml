//! # payment_core
//!
//! A small payment core library implementing a payment lifecycle state machine.
//!
//! The central type is [`Payment`], which holds an amount, a currency, and a
//! lifecycle status. Operations such as [`authorize`](Payment::authorize),
//! [`capture`](Payment::capture), [`refund`](Payment::refund) and
//! [`void`](Payment::void) move the payment through well-defined status
//! transitions and append an entry to an immutable, time-stamped event journal
//! ([`PaymentEvent`]).
//!
//! # Example
//!
//! ```
//! use payment_core::{Currency, Payment, PaymentStatus};
//!
//! let mut payment = Payment::new("inv_1", 2_500, Currency::EUR)?;
//!
//! payment.authorize()?;
//! assert_eq!(payment.status(), PaymentStatus::Authorized);
//!
//! payment.capture(2_500)?;
//! payment.refund(2_500)?;
//! assert_eq!(payment.status(), PaymentStatus::Refunded);
//!
//! assert_eq!(payment.events().len(), 4);
//! # Ok::<(), payment_core::PaymentError>(())
//! ```
//!
//! Amounts are always expressed in the smallest currency unit (minor units),
//! e.g. cents for `USD` or kopecks for `RUB`, and stored as unsigned integers
//! to avoid floating point rounding errors.

mod currency;
mod error;
mod event;
mod payment;
mod status;

pub use currency::Currency;
pub use error::{PaymentError, PaymentResult};
pub use event::{PaymentEvent, PaymentEventType};
pub use payment::{Amount, Payment};
pub use status::PaymentStatus;
