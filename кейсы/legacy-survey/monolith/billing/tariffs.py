"""Тарифы биллинга: комиссия за приём платежа."""

BASE_COMMISSION_PCT = 1.2


def with_commission(amount: float) -> float:
    """Сумма с комиссией, округление до копеек."""
    return round(amount * (1 + BASE_COMMISSION_PCT / 100), 2)
