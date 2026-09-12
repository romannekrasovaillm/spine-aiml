"""Сквозной интеграционный гейт: десять модулей, написанных параллельно
десятью независимыми исполнителями (Claude Code, по одному на worktree),
работают вместе без доработок — стыки держит spine (AD-1…AD-10).
"""

import bankcalc
from bankcalc import (
    backoff,
    calc_fee,
    convert,
    daily_limit_key,
    is_weekend,
    payment_id,
    round_money,
    summarize,
    validate_amount,
    vat,
)


def test_payment_day_pipeline():
    """Сквозной сценарий «день процессинга» (фактический прогон кейса):
    платёж проходит все десять модулей по цепочке."""
    amount, limit, bps, rate, vat_rate = 100.0, 500.0, 50.0, 91.5, 20.0
    assert validate_amount(amount, limit) is True
    fee = calc_fee(amount, bps)
    assert fee == 0.5
    tax = vat(amount, vat_rate)
    assert tax == 20.0
    rub = convert(amount + fee + tax, rate)
    assert rub == 11025.75
    assert round_money(rub) == "11025.75"
    assert payment_id("SBP", 42) == "SBP-000042"
    assert is_weekend(5) is True and is_weekend(2) is False
    assert backoff(3, 0.5) == 4.0
    assert daily_limit_key("C-001", "2026-08-16") == "C-001:2026-08-16"
    assert summarize([amount, fee, tax]) == "count=3; total=120.50"


def test_ten_single_public_contracts():
    """AD-1…AD-10: ровно один публичный контракт на модуль — стык не разошёлся."""
    contracts = {
        "amount": "validate_amount",
        "money": "round_money",
        "fee": "calc_fee",
        "rate": "convert",
        "ids": "payment_id",
        "calendar": "is_weekend",
        "retry": "backoff",
        "tax": "vat",
        "limit": "daily_limit_key",
        "summary": "summarize",
    }
    for mod_name, contract in contracts.items():
        module = getattr(bankcalc, mod_name)
        public = [n for n in dir(module) if not n.startswith("_")]
        assert contract in public, f"{mod_name}: нет {contract}"
        assert [n for n in public if callable(getattr(module, n))] == [contract], (
            f"{mod_name}: публичные {public}"
        )
