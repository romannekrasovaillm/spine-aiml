from billing.tariffs import with_commission


def test_commission_applied():
    assert with_commission(100.0) == 101.2


def test_rounding_to_kopecks():
    assert with_commission(0.01) == 0.01
