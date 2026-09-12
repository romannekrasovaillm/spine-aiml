"""Тесты для spinecalc.report (AD-3)."""

from spinecalc.report import build_report


def test_two_amounts():
    assert build_report([10.5, 20.0]) == "10.5\n20.0\n"


def test_empty_list_returns_empty_string():
    assert build_report([]) == ""


def test_single_amount():
    assert build_report([20.0]) == "20.0\n"


def test_float_values_keep_decimal_representation():
    assert build_report([1.0, 2.5, 3.25]) == "1.0\n2.5\n3.25\n"
