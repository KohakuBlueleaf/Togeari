"""B1 checks behavior, independent of machine speed or scheduler delays."""

from types import SimpleNamespace

import pytest

from togeari.testing.check import _b1, _SubmitBound


@pytest.mark.parametrize("seconds", [0.00001, 0.06, 60.0])
@pytest.mark.parametrize("inline_ok", [0, 1])
def test_only_inline_success_affects_b1(seconds, inline_ok):
    # Synthetic durations verify policy without measuring this machine.
    rules = {}
    limit = _SubmitBound(bound_s=0.002, quantile=0.99, max_s=0.05)
    _b1(SimpleNamespace(submit_s=[seconds] * 100, inline_ok=inline_ok), rules, limit)
    assert rules["B1"].passed is (inline_ok == 0)
