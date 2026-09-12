//! ISO-4217 currency support.

/// Defines a currency enum together with its three-letter ISO-4217 code
/// mapping, `Display` (uppercase code), and case-insensitive `FromStr`.
macro_rules! define_currency {
    (
        $(#[$meta:meta])*
        $name:ident {
            $($variant:ident => $code:literal),+ $(,)?
        }
    ) => {
        $(#[$meta])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
        pub enum $name {
            $($variant),+
        }

        impl $name {
            /// Returns the three-letter ISO-4217 code of this currency, e.g. `"USD"`.
            pub fn code(self) -> &'static str {
                match self {
                    $( $name::$variant => $code ),+
                }
            }
        }

        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str(self.code())
            }
        }

        impl std::str::FromStr for $name {
            type Err = crate::error::PaymentError;

            fn from_str(s: &str) -> Result<Self, Self::Err> {
                match s.trim().to_ascii_uppercase().as_str() {
                    $( $code => Ok($name::$variant), )+
                    other => Err(crate::error::PaymentError::UnknownCurrency(other.to_owned())),
                }
            }
        }

        impl TryFrom<&str> for $name {
            type Error = crate::error::PaymentError;

            fn try_from(value: &str) -> Result<Self, Self::Error> {
                value.parse()
            }
        }

        impl TryFrom<String> for $name {
            type Error = crate::error::PaymentError;

            fn try_from(value: String) -> Result<Self, Self::Error> {
                value.as_str().parse()
            }
        }
    };
}

define_currency! {
    /// An ISO-4217 currency code.
    Currency {
        USD => "USD",
        EUR => "EUR",
        RUB => "RUB",
        GBP => "GBP",
        JPY => "JPY",
        CNY => "CNY",
        CHF => "CHF",
        CAD => "CAD",
        AUD => "AUD",
        NZD => "NZD",
        SEK => "SEK",
        NOK => "NOK",
        DKK => "DKK",
        PLN => "PLN",
        CZK => "CZK",
        HUF => "HUF",
        TRY => "TRY",
        INR => "INR",
        BRL => "BRL",
        MXN => "MXN",
        KRW => "KRW",
        SGD => "SGD",
        HKD => "HKD",
        ZAR => "ZAR",
        AED => "AED",
        KZT => "KZT",
        UAH => "UAH",
        BYN => "BYN",
        AMD => "AMD",
        KGS => "KGS",
        UZS => "UZS",
        GEL => "GEL",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::PaymentError;

    #[test]
    fn parses_case_insensitive_and_trims() {
        assert_eq!("usd".parse::<Currency>().unwrap(), Currency::USD);
        assert_eq!("EUR".parse::<Currency>().unwrap(), Currency::EUR);
        assert_eq!(" rub ".parse::<Currency>().unwrap(), Currency::RUB);
    }

    #[test]
    fn rejects_unknown_code() {
        let err = "XXX".parse::<Currency>().unwrap_err();
        assert_eq!(err, PaymentError::UnknownCurrency("XXX".to_string()));
    }

    #[test]
    fn displays_uppercase_code() {
        assert_eq!(Currency::JPY.to_string(), "JPY");
        assert_eq!(Currency::GBP.code(), "GBP");
    }

    #[test]
    fn converts_from_string_and_str() {
        assert_eq!(
            Currency::try_from("usd".to_string()).unwrap(),
            Currency::USD
        );
        assert_eq!(Currency::try_from("cny").unwrap(), Currency::CNY);
    }
}
