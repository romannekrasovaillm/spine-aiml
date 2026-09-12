def validate_amount(value: float, limit: float) -> bool:
    """Вернуть True, если сумма допустима: неотрицательна и не превышает лимит.

    Отрицательные суммы запрещены (value < 0), превышение лимита отклоняется
    (value > limit). Всё остальное, включая границу value == limit, — True.
    """
    if value < 0:
        return False
    if value > limit:
        return False
    return True
