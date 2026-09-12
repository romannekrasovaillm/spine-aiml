from bankcalc.ids import payment_id


def test_payment_id_sbp_42():
    assert payment_id("SBP", 42) == "SBP-000042"


def test_payment_id_zero_seq():
    assert payment_id("SBP", 0) == "SBP-000000"


def test_payment_id_no_truncation():
    assert payment_id("SBP", 1234567) == "SBP-1234567"


def test_payment_id_other_prefix():
    assert payment_id("VISA", 7) == "VISA-000007"
