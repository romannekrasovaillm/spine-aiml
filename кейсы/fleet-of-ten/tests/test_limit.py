from bankcalc.limit import daily_limit_key


def test_basic_key():
    assert daily_limit_key("alice", "2026-08-16") == "alice:2026-08-16"


def test_empty_day():
    assert daily_limit_key("alice", "") == "alice:"


def test_client_with_hyphen():
    assert daily_limit_key("acme-corp", "2026-08-16") == "acme-corp:2026-08-16"


def test_spaces_preserved():
    assert daily_limit_key("Alice Smith", "2026 08 16") == "Alice Smith:2026 08 16"
