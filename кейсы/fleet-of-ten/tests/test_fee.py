from bankcalc.fee import calc_fee


def test_100_amount_50_bps_is_half():
    assert calc_fee(100, 50) == 0.5


def test_zero_amount():
    assert calc_fee(0, 50) == 0.0


def test_zero_rate():
    assert calc_fee(100, 0) == 0.0


def test_1000_amount_12_5_bps():
    assert calc_fee(1000, 12.5) == 1.25
