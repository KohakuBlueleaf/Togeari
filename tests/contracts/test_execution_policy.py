"""Execution policy is an immutable, validated binding contract, not semantic IR."""

import math
import pickle
from dataclasses import FrozenInstanceError, asdict
from fractions import Fraction

import pytest

from togeari import ExecutionPolicy, Inline, Use
from togeari.core.contracts import ExecutionPolicy as ContractPolicy
from togeari.core.program.bind import Bound


@pytest.mark.parametrize("timeout", [None, 0, 0.0, -0.0, 1, 0.25, Fraction(1, 4)])
def test_timeout_policy_accepts_none_and_finite_nonnegative_real_numbers(timeout):
    policy = ExecutionPolicy(timeout)
    assert policy.timeout_s == (None if timeout is None else float(timeout))
    assert policy.timeout_s is None or type(policy.timeout_s) is float
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        restored = pickle.loads(pickle.dumps(policy, protocol=protocol))
        assert restored == policy
        assert hash(restored) == hash(policy)
        assert asdict(restored) == {"timeout_s": policy.timeout_s}
    with pytest.raises(TypeError, match="reinitialized"):
        policy.__init__(2)
    with pytest.raises(TypeError, match="cannot be replaced"):
        policy.__setstate__([2])
    with pytest.raises(AttributeError):
        _ = policy.__dict__
    with pytest.raises(FrozenInstanceError):
        policy.timeout_s = 2


@pytest.mark.parametrize("timeout", [True, False, "1", "", object(), [], {}, 1 + 0j])
def test_timeout_policy_rejects_bool_and_nonnumeric_values(timeout):
    with pytest.raises(TypeError, match="real number"):
        ExecutionPolicy(timeout)


@pytest.mark.parametrize(
    "timeout",
    [-1, -0.001, math.inf, -math.inf, math.nan, 10**5000, Fraction(-1, 10**1000)],
    ids=lambda _: "invalid",
)
def test_timeout_policy_rejects_negative_nonfinite_and_overflow(timeout):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        ExecutionPolicy(timeout)


def test_public_exports_and_positional_binding_compatibility():
    assert ContractPolicy is ExecutionPolicy
    target, adapter = object(), object()
    binding = Use(target, adapter, "content", True)
    assert binding.policy is None
    assert (binding.target, binding.adapter, binding.store, binding.idempotent) == (
        target,
        adapter,
        "content",
        True,
    )
    policy = ExecutionPolicy(2)
    assert Use(target, adapter, None, False, policy).policy is policy
    bound = Bound("*", False, None, target, adapter, "content", True, 100)
    assert bound.policy == ExecutionPolicy() and bound.policy_source == "none"
    assert bound.warn_us == 100 and bound.idempotent is True
    assert Bound("*", False, policy=policy, policy_source="binding").policy is policy
    with pytest.raises(TypeError):
        Inline(policy=policy)
