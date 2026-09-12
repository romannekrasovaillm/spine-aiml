from bankcalc.calendar import is_weekend


def test_monday_is_not_weekend():
    assert is_weekend(0) is False


def test_friday_is_not_weekend():
    assert is_weekend(4) is False


def test_saturday_is_weekend():
    assert is_weekend(5) is True


def test_sunday_is_weekend():
    assert is_weekend(6) is True


def test_all_weekdays():
    assert [is_weekend(d) for d in range(7)] == [
        False, False, False, False, False, True, True,
    ]
