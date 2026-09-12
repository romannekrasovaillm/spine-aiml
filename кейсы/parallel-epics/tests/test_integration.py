"""Сквозной интеграционный тест: три модуля, написанные параллельно тремя
независимыми исполнителями (Claude Code, по одному на worktree), собираются
в единый пакет и работают вместе без доработок — стыки держит spine
(AD-1…AD-3), а не взаимная видимость исполнителей.
"""

from spinecalc import build_report, format_log, validate_amount


def test_pipeline_end_to_end():
    """Сквозной сценарий: валидация → лог → отчёт (фактический прогон кейса)."""
    amounts = [10.5, 20.0, -5.0, 30.0]
    valid = [a for a in amounts if validate_amount(a, limit=25.0)]
    log = format_log("INFO", f"processed {len(valid)} of {len(amounts)} amounts")
    report = build_report(valid)
    assert valid == [10.5, 20.0]
    assert log == "[INFO] processed 2 of 4 amounts"
    assert report == "10.5\n20.0\n"


def test_single_public_contracts():
    """AD-1…AD-3: ровно одна публичная функция на модуль — стык не разошёлся."""
    import spinecalc.amount as amount
    import spinecalc.logfmt as logfmt
    import spinecalc.report as report

    for module, contract in [
        (amount, "validate_amount"),
        (logfmt, "format_log"),
        (report, "build_report"),
    ]:
        public = [n for n in dir(module) if not n.startswith("_")]
        assert contract in public
        assert [n for n in public if callable(getattr(module, n))] == [contract]
