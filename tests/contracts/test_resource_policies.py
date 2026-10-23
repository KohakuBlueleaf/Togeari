"""Pure resource selection, estimate validation, admission, and observable-policy identity."""

import sys
from dataclasses import FrozenInstanceError
from fractions import Fraction
from types import SimpleNamespace

import pytest

from togeari import Lane, Machine, Use
from togeari.core.contracts.resources import Estimate, EstimateError, ResourceBudgetExceeded
from togeari.core.contracts.results import ItemError
from togeari.core.policies.admission import validate_resource_admission
from togeari.core.policies.resources import resource_former, resource_policy
from togeari.core.program.execution import compatibility, current_execution, execution_context
from togeari.policies import All, Bucketed, BudgetFormer, Fixed, ResourcePressure
from togeari.testing import SimEngine


class Item:
    def __init__(self, **costs):
        self.inputs = costs
        self.ext = {"enqueued": 0.0}
        self._resource_estimates = {}

    def payload(self):
        raise AssertionError("resource policy must not prepare payload")


class Bucket:
    cap = 32

    def __init__(self, items, due=0.0, eligible=None):
        self._items = items
        self.due = due
        self.eligible = items if eligible is None else eligible
        self.requested = []

    def __len__(self):
        return len(self._items)

    def candidates(self, limit):
        self.requested.append(limit)
        yield from self.eligible[:limit]

    def items(self):
        raise AssertionError("no full bucket scan/sort")


UNIT = SimpleNamespace(max_batch=32, key=lambda item: "engine-key")


def estimator(item):
    return item.inputs


def prepared(former, *costs):
    items = [Item(**cost) for cost in costs]
    for item in items:
        assert isinstance(former.prepare(item), Estimate)
    return items


def test_multidimensional_exact_prefix_and_no_best_fit_bypass():
    former = BudgetFormer({"bytes": 10, "tokens": 5}, estimator, size=8, wait_ms=0)
    items = prepared(
        former, {"bytes": 6, "tokens": 2}, {"bytes": 4, "tokens": 3}, {"bytes": 0, "tokens": 0}
    )
    assert former.take(Bucket(items), UNIT, 0) == items
    items = prepared(
        former, {"bytes": 6, "tokens": 1}, {"bytes": 5, "tokens": 1}, {"bytes": 1, "tokens": 1}
    )
    assert former.take(Bucket(items), UNIT, 0) == items[:1]
    assert former.key(items[0], UNIT) == "engine-key"


def test_small_costs_cannot_disappear_from_large_hard_budget():
    budget = 2**54
    former = BudgetFormer({"bytes": budget}, estimator, size=10, wait_ms=0)
    items = prepared(former, *({"bytes": cost} for cost in [1, 1, 1, 1, budget]))
    assert former.take(Bucket(items), UNIT, 0) == items[:4]
    assert former.select(iter(items), 10) == items[:4]
    assert former.select(iter(items), 2) == items[:2]


def test_estimate_constructor_copies_validates_and_blocks_reinitialization():
    values = [["z", 1], ["a", 0]]
    estimate = Estimate(object(), values)
    values[0][1] = 99
    assert estimate.costs == (("a", 0.0), ("z", 1.0))
    for bad in ((("bytes", -1),), (("bytes", float("nan")),), (("x", 1), ("x", 2))):
        with pytest.raises(ValueError):
            Estimate(object(), bad)
    with pytest.raises(TypeError):
        estimate.__init__(object(), (("x", 0),))
    former = BudgetFormer({"bytes": 1}, estimator)
    assert isinstance(former.prepare(Item(bytes=Fraction(1, 10**500))), ItemError)


def test_selector_refused_prefix_never_enters_resource_sum_and_count_cap():
    former = BudgetFormer({"bytes": 10}, estimator, size=3, wait_ms=0)
    items = prepared(former, {"bytes": 10}, {"bytes": 3}, {"bytes": 4}, {"bytes": 1})
    bucket = Bucket(items, eligible=items[1:])
    assert former.take(bucket, SimpleNamespace(max_batch=2), 0) == items[1:3]
    assert bucket.requested == [2]


def test_selection_is_bounded_and_rechecks_forced_estimates():
    former = BudgetFormer({"bytes": 10}, estimator, size=2, wait_ms=100)
    items = prepared(former, {"bytes": 3}, {"bytes": 4}, {"bytes": 1})
    seen = []

    def candidates():
        for item in items:
            seen.append(item)
            yield item

    assert former.select(candidates(), 10) == items[:2]
    assert seen == items[:2]
    items[0].inputs["bytes"] = 8
    former.prepare(items[0], force=True)
    assert former.select(items, 2) == items[:1]


def test_count_or_wait_uses_summaries_without_resource_scan():
    former = BudgetFormer({"bytes": 10}, estimator, size=4, wait_ms=5)
    items = prepared(former, {"bytes": 1}, {"bytes": 1})
    bucket = Bucket(items, due=0.005)
    assert former.deadline(bucket, 0) == 0.005
    assert former.take(bucket, UNIT, 0.004) == []
    assert bucket.requested == []
    assert former.take(bucket, UNIT, 0.005) == items
    full = Bucket(items * 2, due=10)
    assert former.deadline(full, 0) == float("-inf")


def test_cache_per_item_generation_retry_and_force():
    calls = []

    def count(item):
        calls.append(item)
        return item.inputs

    former = BudgetFormer({"bytes": 10}, count)
    first, second = Item(bytes=3), Item(bytes=3)
    estimate = former.prepare(first)
    assert former.prepare(first) is estimate
    assert former.prepare(second) == estimate  # Equal estimate values, distinct cached items.
    assert calls == [first, second]
    first.inputs["bytes"] = 7
    assert former.prepare(first).as_dict() == {"bytes": 3.0}
    assert former.prepare(first, force=True).as_dict() == {"bytes": 7.0}
    replacement = BudgetFormer({"bytes": 10}, count)
    assert replacement.generation is not former.generation
    assert replacement.prepare(first).generation is replacement.generation
    assert calls == [first, second, first, first]


@pytest.mark.parametrize(
    "cost", [-1, -Fraction(1, 10**500), float("nan"), float("inf"), True, "3", 10**500]
)
def test_invalid_cost_is_terminal_error_and_cached(cost):
    former = BudgetFormer({"bytes": 10}, estimator)
    item = Item(bytes=cost)
    result = former.prepare(item)
    assert isinstance(result, ItemError) and isinstance(result.error, EstimateError)
    assert former.prepare(item) is result


def test_missing_dimension_is_not_zero_and_estimator_exception_contained():
    former = BudgetFormer({"bytes": 10, "tokens": 2}, estimator)
    result = former.prepare(Item(bytes=0))
    assert isinstance(result, ItemError) and "tokens" in str(result.error)

    def fail(item):
        raise RuntimeError("bad metadata")

    failed = BudgetFormer({"bytes": 10}, fail)
    assert isinstance(failed.prepare(Item()).error, EstimateError)
    assert isinstance(former.prepare(SimpleNamespace(inputs={})).error, EstimateError)


def test_oversized_reject_or_explicit_singleton_never_mixes_items():
    reject = BudgetFormer({"bytes": 10}, estimator)
    item = Item(bytes=11)
    error = reject.prepare(item).error
    assert isinstance(error, ResourceBudgetExceeded)
    assert error.estimate.costs == (("bytes", 11.0),)
    assert error.estimate.oversized
    assert error.costs == {"bytes": 11.0} and error.budgets == {"bytes": 10.0}
    assert error.oversized_dimensions == ("bytes",)
    assert "bytes: estimate=11, budget=10" in str(error)
    item.inputs["bytes"] = 0
    assert error.costs["bytes"] == 11.0
    with pytest.raises(TypeError):
        error.costs["bytes"] = 0
    with pytest.raises(AttributeError):
        error.estimate = None
    singleton = BudgetFormer({"bytes": 10}, estimator, wait_ms=0, oversized="singleton")
    items = prepared(singleton, {"bytes": 11}, {"bytes": 1})
    assert singleton.prepare(items[0]).oversized
    assert singleton.take(Bucket(items), UNIT, 0) == items[:1]
    assert singleton.take(Bucket(list(reversed(items))), UNIT, 0) == items[1:]


def test_unprepared_or_failed_candidate_is_failclosed():
    former = BudgetFormer({"bytes": 10}, estimator, wait_ms=0)
    item = Item(bytes=1)
    assert former.take(Bucket([item]), UNIT, 0) == []
    bad = Item(bytes=-1)
    former.prepare(bad)
    assert former.take(Bucket([bad]), UNIT, 0) == []


@pytest.mark.parametrize(
    "budgets",
    [
        {},
        {"": 1},
        {1: 1},
        {"x": 0},
        {"x": -1},
        {"x": True},
        {"x": float("inf")},
        {"x": Fraction(1, 10**500)},
    ],
)
def test_invalid_budgets_rejected(budgets):
    with pytest.raises(ValueError):
        BudgetFormer(budgets, estimator)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"size": 0},
        {"size": True},
        {"wait_ms": -1},
        {"wait_ms": -Fraction(1, 10**500)},
        {"oversized": "wait"},
        {"estimator_revision": ""},
        {"estimator_revision": 1},
    ],
)
def test_other_invalid_config_rejected(kwargs):
    with pytest.raises((TypeError, ValueError)):
        BudgetFormer({"x": 1}, estimator, **kwargs)


def test_configuration_and_estimates_immutable_and_descriptor_detached():
    budgets = {"bytes": 10}
    former = BudgetFormer(budgets, estimator)
    budgets["bytes"] = 20
    assert former.budgets["bytes"] == 10
    with pytest.raises(TypeError):
        former.budgets["bytes"] = 20
    with pytest.raises(FrozenInstanceError):
        former.size = 100
    with pytest.raises(TypeError):
        former.__init__({"bytes": 20}, estimator)
    estimate = former.prepare(Item(bytes=1))
    with pytest.raises(FrozenInstanceError):
        estimate.oversized = True
    descriptor = resource_policy(former)
    descriptor["budgets"]["bytes"] = 100
    assert former.budgets["bytes"] == 10
    wrapped = Bucketed(Bucketed(former, lambda item: "a"), lambda item: "b")
    assert resource_former(wrapped) is former
    assert resource_policy(wrapped) == resource_policy(former)


def signals(total=None, unknown=0):
    return {
        "lanes": {
            "work": {
                "resources": {
                    "total": {"bytes": 0} if total is None else total,
                    "unknown_items": unknown,
                }
            }
        }
    }


def test_resource_pressure_new_admission_and_diagnostics():
    policy = ResourcePressure("work", {"bytes": 10}, max_jobs=3)
    pool = SimpleNamespace(jobs=1)
    assert policy.allows(pool, signals({"bytes": 9}))
    assert policy.status(pool, signals({"bytes": 10}))["reason"] == "resource_limit"
    assert policy.status(pool, signals({"tokens": 1}))["reason"] == "missing_dimension"
    assert policy.status(pool, signals(unknown=1))["reason"] == "unknown_resource_estimates"
    assert policy.status(pool, {"lanes": {}})["reason"] == "missing_lane"
    assert (
        policy.status(pool, signals({"bytes": float("nan")}))["reason"] == "invalid_resource_total"
    )
    assert policy.status(SimpleNamespace(jobs=3), signals())["reason"] == "max_jobs"
    assert policy.allows(SimpleNamespace(jobs=0), signals())


def test_resource_pressure_overflow_is_a_known_limit_not_unknown_cost():
    policy = ResourcePressure("work", {"bytes": sys.float_info.max})
    pool = SimpleNamespace(jobs=0)
    snapshot = signals({"bytes": sys.float_info.max})
    resources = snapshot["lanes"]["work"]["resources"]
    resources["overflow_dimensions"] = ["bytes"]
    assert policy.status(pool, snapshot) == {
        "allow": False,
        "reason": "resource_limit",
        "dimension": "bytes",
        "overflow": True,
    }
    # An unrelated overflow dimension does not block this configured admission limit.
    resources["overflow_dimensions"] = ["tokens"]
    resources["total"]["bytes"] = 1.0
    assert policy.allows(pool, snapshot)
    resources["overflow_dimensions"] = []
    assert policy.allows(pool, snapshot)
    resources["overflow_dimensions"] = "bytes"
    assert policy.status(pool, snapshot)["reason"] == "invalid_overflow_dimensions"


def test_resource_admission_construction_validation_known_compositions_only():
    budget = BudgetFormer({"bytes": 10}, estimator)
    lane = SimpleNamespace(name="work", former=Bucketed(budget, lambda item: "key"))
    valid = ResourcePressure("work", {"bytes": 20})
    assert validate_resource_admission(All(Fixed(2), All(valid)), [lane]) == []
    assert validate_resource_admission(ResourcePressure(lane, {"bytes": 1}), [lane]) == []
    assert (
        "missing" in validate_resource_admission(ResourcePressure("typo", {"bytes": 1}), [lane])[0]
    )
    assert (
        "unestimated"
        in validate_resource_admission(ResourcePressure("work", {"tokens": 1}), [lane])[0]
    )
    assert (
        "BudgetFormer"
        in validate_resource_admission(valid, [SimpleNamespace(name="work", former=None)])[0]
    )
    assert "ambiguous" in validate_resource_admission(valid, [lane, lane])[0]
    other = SimpleNamespace(name="work", former=budget)
    assert (
        "missing" in validate_resource_admission(ResourcePressure(other, {"bytes": 1}), [lane])[0]
    )
    cycle = All()
    cycle.rules = (cycle,)
    assert "cyclic" in validate_resource_admission(cycle, [lane])[0]

    class Custom:
        @property
        def rules(self):
            raise AssertionError("do not inspect arbitrary user policies")

    assert validate_resource_admission(Custom(), [lane]) == []


def test_observable_budget_identity_and_revision_coverage():
    machines = []

    def make(former, bundle=None):
        lane = Lane([SimEngine("batch", name="unit")], former=former)
        m = Machine(
            program="(1)identity(x)",
            bind={"*": Use(lane, binding_revision=bundle)},
            core="python",
            start_units=False,
        )
        machines.append(m)
        return m, lane

    def manifest(m):
        return current_execution(m.code.regions[0], m.code, m)

    try:
        a, lane = make(
            BudgetFormer({"bytes": 10}, estimator, estimator_revision="cost/1"), "bundle"
        )
        b, _ = make(
            BudgetFormer({"bytes": 10}, estimator, size=8, wait_ms=20, estimator_revision="cost/1"),
            "bundle",
        )
        original = manifest(a)
        assert original["hash"] == manifest(b)["hash"]
        assert original["completeness"]["complete"]
        lane.former = BudgetFormer({"bytes": 20}, estimator, estimator_revision="cost/1")
        changed = manifest(a)
        assert changed["hash"] != original["hash"]
        active = execution_context(a.code.regions[0], a.code, a, original["epoch"])
        assert not compatibility(active, active, "warn")["allow"]
        unknown, _ = make(BudgetFormer({"bytes": 10}, estimator))
        assert any(
            "estimator_revision" in gap["path"]
            for gap in manifest(unknown)["completeness"]["unknown"]
        )
        attested, attested_lane = make(BudgetFormer({"bytes": 10}, estimator), "covers-estimator")
        before = manifest(attested)
        assert before["completeness"]["complete"]
        attested_lane.former = BudgetFormer({"bytes": 10}, estimator, size=1, wait_ms=0)
        assert manifest(attested) is before  # Generation/count/wait alone are not identity.
        attested_lane.former = BudgetFormer({"bytes": 10}, lambda item: item.inputs)
        replaced = manifest(attested)
        assert replaced["hash"] == before["hash"]  # Local callable IDs never persist.
        assert replaced["epoch"] > before["epoch"]
        assert not replaced["completeness"]["complete"]
        assert any(
            "estimator changed" in gap["reason"] for gap in replaced["completeness"]["unknown"]
        )
        attested_lane.former = BudgetFormer({"bytes": 10}, estimator)
        assert not manifest(attested)["completeness"]["complete"]  # Sticky after change back.
        attested_lane.former = BudgetFormer({"bytes": 10}, estimator, estimator_revision="cost/2")
        assert manifest(attested)["completeness"]["complete"]
    finally:
        for m in machines:
            m.stop()
