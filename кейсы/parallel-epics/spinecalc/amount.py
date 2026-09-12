"""Модуль валидации сумм: единственный публичный контракт — validate_amount.

AD-1 (ARCHITECTURE-SPINE.md): ровно одна публичная функция, без print,
без доступа к сети/файлам.
"""


def validate_amount(value, limit) -> bool:
    """Проверить сумму на допустимость относительно лимита.

    Правила:
      - value < 0      -> False (отрицательные суммы запрещены)
      - value > limit  -> False (превышение лимита)
      - иначе          -> True
    """
    return value >= 0 and value <= limit
