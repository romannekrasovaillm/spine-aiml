from bankcalc.amount import validate_amount


def test_negative_amount_is_rejected():
    assert validate_amount(-0.01, 100.0) is False


def test_amount_above_limit_is_rejected():
    assert validate_amount(100.01, 100.0) is False


def test_amount_within_limit_is_accepted():
    assert validate_amount(50.0, 100.0) is True


def test_amount_equal_to_limit_is_accepted():
    assert validate_amount(100.0, 100.0) is True


def test_zero_amount_is_accepted():
    assert validate_amount(0.0, 100.0) is True
