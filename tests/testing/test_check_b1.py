"""B1's submit-time bounds: the quantile bound applies once there are enough submits for it."""

from types import SimpleNamespace

from togeari.testing.check import _b1, _SubmitBound

LIMIT = _SubmitBound(bound_s=0.002, quantile=0.99, max_s=0.05)


def verdict(times_ms: list[float]) -> bool:
    rules: dict = {}
    _b1(SimpleNamespace(submit_s=[t / 1000.0 for t in times_ms], inline_ok=0), rules, LIMIT)
    return rules["B1"].passed


def test_one_slow_submit_among_too_few_for_the_quantile_is_held_to_the_max_bound():
    # 47 submits: the q0.99 is the slowest one; only the max bound applies
    assert verdict([0.05] * 46 + [2.1])
    assert not verdict([0.05] * 46 + [60.0])


def test_the_quantile_bound_applies_once_there_are_enough_submits():
    # 100 submits: q0.99 is the 99th; two slow ones put it above the bound
    assert verdict([0.05] * 99 + [2.1])
    assert not verdict([0.05] * 98 + [2.1, 2.1])
