"""B1's sample size per quantile: exactly 1 / (1 - quantile) submits, free of float error."""

from types import SimpleNamespace

from togeari.testing.check import _b1, _SubmitBound


def passed(quantile: float, times_ms: list[float]) -> bool:
    limit = _SubmitBound(bound_s=0.002, quantile=quantile, max_s=0.05)
    rules: dict = {}
    _b1(SimpleNamespace(submit_s=[t / 1000.0 for t in times_ms], inline_ok=0), rules, limit)
    return rules["B1"].passed


def test_q0_9_applies_its_bound_from_ten_submits():
    # Ten submits, two slow: q0.9 is the 9th, slow, so the bound applies and fails
    assert not passed(0.9, [0.05] * 8 + [2.1, 2.1])

    # Nine submits: too few for q0.9, only the max bound applies
    assert passed(0.9, [0.05] * 7 + [2.1, 2.1])
