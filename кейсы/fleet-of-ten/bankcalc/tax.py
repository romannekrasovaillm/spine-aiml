def vat(amount: float, rate: float) -> float:
    """НДС сверху: amount × rate / 100."""
    return amount * rate / 100
