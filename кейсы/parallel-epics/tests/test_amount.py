"""Тесты модуля spinecalc.amount.validate_amount."""

from spinecalc.amount import validate_amount


def test_negative_amount_rejected():
    """Отрицательная сумма запрещена."""
    assert validate_amount(-1, 100) is False


def test_amount_above_limit_rejected():
    """Сумма, превышающая лимит, запрещена."""
    assert validate_amount(101, 100) is False


def test_amount_within_limit_accepted():
    """Сумма в пределах лимита допустима."""
    assert validate_amount(50, 100) is True


def test_amount_equal_to_limit_accepted():
    """Граничное значение (равно лимиту) допустимо."""
    assert validate_amount(100, 100) is True


def test_zero_amount_accepted():
    """Нулевая сумма допустима (не отрицательна и не превышает лимит)."""
    assert validate_amount(0, 100) is True
