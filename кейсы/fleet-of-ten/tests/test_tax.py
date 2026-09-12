from bankcalc.tax import vat


def test_vat_20_percent_of_100():
    assert vat(100.0, 20.0) == 20.0


def test_vat_zero_rate():
    assert vat(100.0, 0.0) == 0.0


def test_vat_zero_amount():
    assert vat(0.0, 20.0) == 0.0


def test_vat_fractional_rate():
    assert vat(100.0, 7.5) == 7.5
