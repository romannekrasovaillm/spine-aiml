def backoff(attempt: int, base: float) -> float:
    """Экспоненциальная задержка повтора: base × 2^attempt."""
    return base * (2 ** attempt)
