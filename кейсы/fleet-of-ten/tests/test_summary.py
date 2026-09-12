from bankcalc.summary import summarize


def test_empty_list():
    assert summarize([]) == "count=0; total=0.00"


def test_single_amount():
    assert summarize([10.5]) == "count=1; total=10.50"


def test_two_amounts():
    assert summarize([10.5, 20.0]) == "count=2; total=30.50"


def test_float_values():
    assert summarize([0.1, 0.2, 0.3]) == "count=3; total=0.60"
