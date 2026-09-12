from bankcalc.rate import convert


def test_convert_basic():
    assert convert(100.0, 1.5) == 150.0


def test_convert_zero_rate():
    assert convert(100.0, 0.0) == 0.0


def test_convert_unit_rate():
    assert convert(42.0, 1.0) == 42.0


def test_convert_fractional_rate():
    assert convert(200.0, 0.25) == 50.0
