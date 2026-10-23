"""Resource-bounded batching, lifecycle accounting and transformed inputs on both cores."""

import json
import sys

import pytest
from kohakunode import NodeLibrary

from togeari import BasePlugin, Lane, Machine, Use
from togeari.conformance import conform
from togeari.core.contracts.adapter import PythonAdapter
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.results import Ok, UnitError
from togeari.core.contracts.views import PoolView
from togeari.core.contracts.workitem import WorkItem
from togeari.core.lanes.deadline import replace as replace_deadline
from togeari.core.lanes.lane import Retry
from togeari.core.lanes.native import NativeLane
from togeari.core.lanes.queue import Bucket
from togeari.policies import BudgetFormer, ResourcePressure, Selector, SizeOrWait
from togeari.testing.trace import ScriptedEngine, run_trace

LIB = NodeLibrary("resource_test")


@LIB.node()
def measure(cost: int, other: int = 1) -> int:
    return cost + other


PROGRAM = """@def (cost: int, other: int = 1)main(value, error):
    value = -1
    error = None
    @try:
        (cost, other)measure(value)
    @except caught:
        error = caught
"""


class Batch(ScriptedEngine):
    def __init__(self, name="batch", **kwargs):
        super().__init__("batch", name=name, capacity=8, max_batch=8, **kwargs)
        self.batches = []

    def delay(self):
        return 0.25

    def key(self, item):
        return item.inputs.get("other", 1)

    def submit(self, items, done):
        self.batches.append([(item.inputs["cost"], item.inputs.get("other", 1)) for item in items])
        return super().submit(items, done)


def estimate(item):
    return {"tokens": item.inputs["cost"], "bytes": item.inputs.get("other", 1)}


def build(core, former, *, plugins=(), forming="late", units=None, select=None, admission=None):
    units = [Batch()] if units is None else units
    lane = Lane(units, former=former, forming=forming, select=select)
    machine = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        core=core,
        bind={"measure": Use(lane, deterministic=True)},
        record=1.0,
        plugins=list(plugins),
        admission=admission,
    )
    assert isinstance(lane, NativeLane) == (core == "rust")
    return machine, lane, units


def budget(**kwargs):
    return BudgetFormer({"tokens": 5, "bytes": 4}, estimate, size=8, wait_ms=0, **kwargs)


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("forming", ["late", "early"])
def test_multidimension_prefix_exactfits_and_ledger_drains(core, forming):
    calls = []

    def counted(item):
        calls.append(id(item))
        return estimate(item)

    former = BudgetFormer({"tokens": 5, "bytes": 4}, counted, size=8, wait_ms=0)
    machine, lane, units = build(core, former, forming=forming)
    assert lane.signals()["resources"]["total"] == {"bytes": 0.0, "tokens": 0.0}
    jobs = [{"cost": n} for n in [2, 3, 4, 0, 1]]
    trace = run_trace(lambda: machine, jobs)
    assert [record.outputs["value"] for record in trace.records] == [3, 4, 5, 1, 2]
    assert len(calls) == len(jobs)
    assert units[0].batches == [[(2, 1), (3, 1)], [(4, 1), (0, 1), (1, 1)]]
    resource = lane.resource_view()
    assert resource["total"] == {"bytes": 0.0, "tokens": 0.0}
    for record in trace.records:
        assert conform(machine.code.entries["main"].region.job, record, jobs[int(record.key)]).ok


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("oversized", ["reject", "singleton"])
def test_oversized_is_explicit_terminal_or_singleton(core, oversized):
    machine, lane, units = build(core, budget(oversized=oversized))
    trace = run_trace(lambda: machine, [{"cost": 9}, {"cost": 2}])
    first = next(record for record in trace.records if record.key == "0")
    if oversized == "reject":
        assert first.outputs["value"] == -1
        assert first.outputs["error"]["kind"] == "ResourceBudgetExceeded"
        assert first.stats["record_invocations"][0]["attempt_ids"] == []
        assert first.stats["policy_failures"][0]["reason"] == "resource_budget"
    else:
        assert first.outputs["value"] == 10
        assert units[0].batches[0] == [(9, 1)]
        assert lane.resource_view()["oversized_batches"] == 1
    assert lane.resource_view()["total"] == {"bytes": 0.0, "tokens": 0.0}


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("bad", [{}, {"tokens": -1}, {"tokens": float("nan")}])
def test_invalid_estimates_fail_before_any_attempt(core, bad):
    former = BudgetFormer({"tokens": 5}, lambda item: bad, wait_ms=0)
    machine, lane, units = build(core, former)
    [record] = run_trace(lambda: machine, [{"cost": 2}]).records
    assert record.ok and record.outputs["error"]["kind"] == "EstimateError"
    assert units[0].batches == []
    assert record.stats["record_invocations"][0]["attempt_ids"] == []
    assert lane.resource_view()["rejected"] == 1


class Grow(BasePlugin):
    name = "grow-resource"

    def pre_submit(self, item, unit, lane):
        # Stable mutation on repeat preparation; every original input cost was one.
        item.inputs = {**item.inputs, "cost": 3}
        return item


@pytest.mark.parametrize("core", ["python", "rust"])
def test_posthook_reestimate_repackages_without_loss_or_overbudget(core):
    machine, lane, units = build(core, budget(), plugins=[Grow()])
    trace = run_trace(lambda: machine, [{"cost": 1}] * 4)
    assert len(trace.records) == 4 and all(record.outputs["value"] == 4 for record in trace.records)
    assert units[0].batches == [[(3, 1)]] * 4
    assert lane.resource_view()["total"] == {"bytes": 0.0, "tokens": 0.0}


class Refuse(Selector):
    def order(self, units, lane):
        return units

    def admits(self, item, unit, lane):
        return item.inputs["cost"] % 2 == (0 if unit.name == "even" else 1)


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("forming", ["late", "early"])
def test_selector_refused_prefix_does_not_hide_eligible_suffix(core, forming):
    machine, lane, units = build(
        core, budget(), units=[Batch("even"), Batch("odd")], select=Refuse(), forming=forming
    )
    trace = run_trace(lambda: machine, [{"cost": n} for n in [1, 3, 2, 2]])
    assert len(trace.records) == 4 and all(record.ok for record in trace.records)
    assert units[0].batches == [[(2, 1), (2, 1)]]
    assert units[1].batches == [[(1, 1), (3, 1)]]
    assert lane.resource_view()["total"] == {"bytes": 0.0, "tokens": 0.0}


@pytest.mark.parametrize("core", ["python", "rust"])
def test_resource_pressure_initial_known_zero_admits_and_drains(core):
    unit = Batch()
    former = budget()
    lane = Lane([unit], former=former)
    machine = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        core=core,
        bind={"measure": Use(lane)},
        admission=ResourcePressure(lane, {"tokens": 5}, max_jobs=2),
    )
    trace = run_trace(lambda: machine, [{"cost": 2}] * 3)
    assert len(trace.records) == 3 and all(record.ok for record in trace.records)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_large_prefix_does_not_materialize_sorted_bucket_items(core, monkeypatch):
    def forbidden(self):
        raise AssertionError("full bucket sort is forbidden")

    monkeypatch.setattr(Bucket, "items", forbidden)
    machine, lane, units = build(core, budget())
    trace = run_trace(lambda: machine, [{"cost": 1}] * 100)
    assert len(trace.records) == 100
    assert sum(len(batch) for batch in units[0].batches) == 100
    assert all(len(batch) <= 4 for batch in units[0].batches)


class Controlled(EngineBase):
    kind = "batch"
    max_batch = 8
    protocol = "python"

    def __init__(self):
        super().__init__("controlled")
        self.cap = 1
        self.submitted = []

    def capacity(self):
        return self.cap

    def submit(self, items, done):
        self.submitted.append(list(items))
        return len(self.submitted)


def manual(core, former):
    unit = Controlled()
    machine, lane, _ = build(core, former, units=[unit])
    lane.clock = lambda: 0.0
    return machine, lane, unit


def submission(lane):
    [sub] = lane.subs.values()
    return sub.id if isinstance(lane, NativeLane) else sub


def item(cost, other=1):
    return WorkItem("measure", {"cost": cost, "other": other}, PythonAdapter(measure))


@pytest.mark.parametrize("core", ["python", "rust"])
def test_retry_cancel_and_former_swap_preserve_ledger_exactly_once(core):
    calls = []

    def counted(work):
        calls.append(id(work))
        return {"tokens": work.inputs["cost"]}

    policy = BudgetFormer({"tokens": 5}, counted, size=2, wait_ms=0)
    machine, lane, unit = manual(core, policy)
    lane.retry = Retry(2, 500)
    try:
        first, second = item(2), item(3)
        lane.enqueue(first, 0)
        lane.enqueue(second, 0)
        assert lane.resource_view()["queued"] == {"tokens": 5}
        lane.pump(0)
        assert lane.resource_view()["inflight"] == {"tokens": 5}
        assert lane.resource_view()["queued"] == {"tokens": 0}
        lane.results(submission(lane), [UnitError(ConnectionError("retry"))] * 2, 0.1)
        assert lane.resource_view()["retry_held"] == {"tokens": 5}
        assert lane.resource_view()["inflight"] == {"tokens": 0}
        assert len(calls) == 2
        lane.cancel(first)
        assert lane.resource_view()["retry_held"] == {"tokens": 3}
        lane.former = BudgetFormer({"tokens": 2}, counted, size=2, wait_ms=0)
        [(failed, error)] = lane.timers(0.2)
        assert failed is second and type(error.error).__name__ == "ResourceBudgetExceeded"
        assert lane.resource_view()["total"] == {"tokens": 0}
        lane.timers(5)
        lane.pump(5)
        assert len(unit.submitted) == 1 and lane.queued() == 0
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_swap_reestimates_queued_not_inflight_and_preserves_new_dimension_zero(core):
    machine, lane, unit = manual(core, BudgetFormer({"tokens": 10}, estimate, size=1, wait_ms=0))
    try:
        first, second = item(4), item(3)
        lane.enqueue(first, 0)
        lane.pump(0)
        unit.cap = 0
        lane.enqueue(second, 0)
        lane.former = BudgetFormer({"bytes": 4}, estimate, size=1, wait_ms=0)
        view = lane.resource_view()
        assert view["inflight"]["tokens"] == 4
        assert view["queued"]["tokens"] == 0 and view["queued"]["bytes"] == 1
        lane.results(submission(lane), [Ok(5)], 0.5)
        lane.cancel(second)
        assert all(cost == 0 for cost in lane.resource_view()["total"].values())
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_compatibility_keys_wait_and_large_float_bound_stay_safe(core):
    large = 2**54
    policy = BudgetFormer(
        {"tokens": large}, lambda work: {"tokens": work.inputs["cost"]}, size=8, wait_ms=100
    )
    machine, lane, unit = manual(core, policy)
    try:
        values = [item(1) for _ in range(4)] + [item(large)]
        for value in values:
            lane.enqueue(value, 0)
        lane.pump(0.05)
        assert unit.submitted == []
        lane.pump(0.1)
        assert [len(batch) for batch in unit.submitted] == [4]
        assert lane.resource_view(values[-1])["wait_reason"]["reason"] == "budget_prefix"
        for value in values:
            lane.cancel(value)
        assert lane.resource_view()["total"] == {"tokens": 0}
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_estimate_reporting_overflow_is_finite_flagged_and_exactly_drains(core):
    maximum = sys.float_info.max
    former = BudgetFormer(
        {"tokens": maximum}, lambda work: {"tokens": work.inputs["cost"]}, wait_ms=0
    )
    machine, lane, _ = manual(core, former)
    try:
        values = [item(maximum), item(maximum), item(1)]
        for value in values:
            lane.enqueue(value, 0)
        view = lane.resource_view()
        assert view["total"] == {"tokens": maximum} and view["overflow_dimensions"] == ["tokens"]
        json.dumps(view, allow_nan=False)
        aggregate = machine.dispatcher.signals()["resources"]
        assert aggregate["overflow_dimensions"] == ["tokens"]
        pressure = ResourcePressure(lane, {"tokens": maximum})
        assert not pressure.status(
            PoolView(1, 0), {"lanes": {lane.name: lane.signals()}, "resources": aggregate}
        )["allow"]
        for value in values:
            lane.cancel(value)
        assert lane.resource_view()["total"] == {"tokens": 0}
        assert lane.resource_view()["overflow_dimensions"] == []
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_enable_budget_while_inflight_marks_unknown_until_it_settles(core):
    machine, lane, unit = manual(core, SizeOrWait(1, 0))
    try:
        value = item(2)
        lane.enqueue(value, 0)
        lane.pump(0)
        lane.former = budget()
        assert lane.resource_view()["unknown_items"] == 1
        lane.results(submission(lane), [Ok(3)], 0.25)
        assert lane.resource_view()["unknown_items"] == 0
        assert lane.resource_view()["total"] == {"bytes": 0, "tokens": 0}
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("mixed", [False, True])
def test_posthook_expired_replacements_release_original_resource_charges(core, mixed):
    machine, lane, unit = manual(core, budget())
    clock = [0.0]
    lane.clock = lambda: clock[0]

    def hook(lane, unit, items):
        replacements = []
        for original in items:
            replacement = WorkItem(original.node, original.inputs, original.adapter)
            replace_deadline(original, replacement)
            replacements.append(replacement)
        clock[0] = 2.0
        return replacements

    lane.hook_submit = hook
    try:
        first, second = item(2), item(2)
        first.deadline = 1.0
        second.deadline = 4.0 if mixed else 1.0
        lane.enqueue(first, 0)
        lane.enqueue(second, 0)
        lane.pump(0)
        expired = lane.timers(2)
        assert len(expired) == (1 if mixed else 2)
        if mixed:
            assert len(unit.submitted) == 1 and len(unit.submitted[0]) == 1
            assert lane.resource_view()["inflight"] == {"bytes": 1, "tokens": 2}
            lane.results(submission(lane), [Ok(3)], 2.25)
        else:
            assert unit.submitted == []
        assert lane.resource_view()["total"] == {"bytes": 0, "tokens": 0}
        assert lane.queued() == 0 and lane.timers(10) == []
    finally:
        machine.stop()


def test_native_wait_item_is_bounded_readonly_metadata():
    machine, lane, unit = manual("rust", budget())
    try:
        value = item(2)
        lane.enqueue(value, 0)
        before = lane.resource_view()
        empty = lane._wait_item(value, 0, limit=0)
        assert empty["truncated"] and empty["scanned"] == 0 and empty["phase"] == "unknown"
        bounded = lane._wait_item(value, 0, limit=2)
        assert bounded["scanned"] <= 2
        assert "resources" not in bounded
        assert lane.resource_view() == before
    finally:
        machine.stop()
