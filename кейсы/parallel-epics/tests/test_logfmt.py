"""Тесты модуля spinecalc.logfmt (AD-2: единый формат логов)."""

from spinecalc.logfmt import format_log


def test_basic_format():
    assert format_log("INFO", "ok") == "[INFO] ok"


def test_empty_msg():
    assert format_log("INFO", "") == "[INFO] "


def test_empty_level():
    assert format_log("", "ok") == "[] ok"


def test_level_with_spaces():
    assert format_log("DE BU G", "ok") == "[DE BU G] ok"


def test_level_and_msg_with_spaces():
    assert format_log("WARN", "hello world") == "[WARN] hello world"


def test_lowercase_level_is_untouched():
    assert format_log("debug", "x") == "[debug] x"
