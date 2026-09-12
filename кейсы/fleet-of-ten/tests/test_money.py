from bankcalc.money import round_money


def test_half_rounds_to_two_decimals():
    assert round_money(10.5) == "10.50"


def test_integer_gets_two_decimals():
    assert round_money(7) == "7.00"


def test_three_decimals_round():
    assert round_money(10.567) == "10.57"


def test_zero():
    assert round_money(0) == "0.00"
