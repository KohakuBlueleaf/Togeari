"""Legacy timing references do not affect behavioral conformance."""

from types import SimpleNamespace

import pytest

from togeari.testing.check import _b1, _SubmitBound


@pytest.mark.parametrize("quantile", [0.9, 0.99, 1.0])
def test_quantile_is_diagnostic_only(quantile):
    # No timing threshold, including the maximum, changes the verdict.
    limit = _SubmitBound(bound_s=0.002, quantile=quantile, max_s=0.05)
    rules = {}
    _b1(SimpleNamespace(submit_s=[60.0] * 100, inline_ok=0), rules, limit)
    assert rules["B1"].passed
