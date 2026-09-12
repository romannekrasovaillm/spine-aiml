"""Комиссия в базисных пунктах (AD-3)."""


def calc_fee(amount: float, bps: float) -> float:
    """Возвращает комиссию: amount × bps / 10000 (bps — базисные пункты)."""
    return amount * bps / 10000
