def payment_id(prefix: str, seq: int) -> str:
    return f"{prefix}-{seq:06d}"
