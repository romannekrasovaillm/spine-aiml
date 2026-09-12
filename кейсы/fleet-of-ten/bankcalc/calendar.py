"""Календарь выходных (AD-6)."""


def is_weekend(weekday: int) -> bool:
    """Вернуть True, если день — выходной (сб или вс).

    weekday: int, 0=пн … 6=вс.
    """
    return weekday >= 5
