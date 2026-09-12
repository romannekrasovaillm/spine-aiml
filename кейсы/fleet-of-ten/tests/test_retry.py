from bankcalc.retry import backoff


def test_backoff_attempt_zero_returns_base():
    assert backoff(0, 2.0) == 2.0


def test_backoff_attempt_one_doubles_base():
    assert backoff(1, 3.0) == 6.0


def test_backoff_attempt_three():
    assert backoff(3, 1.0) == 8.0


def test_backoff_fractional_base():
    assert backoff(2, 0.25) == 1.0
