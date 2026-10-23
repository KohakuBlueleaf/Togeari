"""Observable call/attempt policies use deterministic clocks and preserve KIR errors."""

import pytest
from kohakunode import NodeLibrary

from togeari import BasePlugin, ExecutionPolicy, Inline, Lane, Machine, Use
from togeari.cli.replay import replay_dir
from togeari.conformance import conform
from togeari.core.contracts.results import ItemTimeout, UnitError
from togeari.core.contracts.workitem import WorkItem
from togeari.core.lanes.deadline import current, replace
from togeari.core.lanes.lane import Retry
from togeari.core.lanes.native import NativeLane
from togeari.plugins import Archive, Recorder
from togeari.policies import Fifo
from togeari.stores import MemoryStore
from togeari.testing.trace import ScriptedEngine, run_trace

LIB = NodeLibrary("execution_policy_test")


@LIB.node()
def inc(x: int) -> int:
    return x + 1


@LIB.node()
def host_timeout(x: int) -> int:
    raise ItemTimeout(f"host timeout {x}")


PROGRAM = """@def (x: int)main(value, error):
    value = -1
    error = None
    @try:
        (x)inc(value)
    @except caught:
        error = caught
"""


class FixedDelay(ScriptedEngine):
    def __init__(self, seconds=1.0, unit_error=False, **kwargs):
        super().__init__("call", **kwargs)
        self.seconds = seconds
        self.unit_error = unit_error
        self.enqueued = []

    def delay(self):
        return self.seconds

    def submit(self, work, done):
        self.enqueued.append((self.schedule.clock(), work.deadline))
        return super().submit(work, done)

    def run_one(self, item, fail):
        if self.unit_error:
            return UnitError(ConnectionError("unit lost"))
        return super().run_one(item, fail)


def execute(
    core,
    *,
    delay=1.0,
    timeout=1.0,
    attempt=None,
    policy=True,
    source=PROGRAM,
    capacity=8,
    jobs=None,
    plugins=(),
    store=None,
    store_mode=None,
):
    unit = FixedDelay(delay, name="unit", capacity=capacity)
    lane = Lane([unit], timeout_s=attempt)
    config = ExecutionPolicy(timeout_s=timeout) if policy else None
    m = Machine(
        program=source,
        entry="main",
        libraries=[LIB],
        bind={"inc": Use(lane, policy=config, store=store_mode), "kir.std.*": Inline()},
        core=core,
        record=1.0,
        plugins=list(plugins),
        store=store,
    )
    events = []
    m.bus.listen(
        lambda kind, fields: events.append((kind, fields)), kinds=["fault.taken", "lane.result"]
    )
    trace = run_trace(lambda: m, [{"x": 3}] if jobs is None else jobs)
    assert isinstance(lane, NativeLane) == (core == "rust")
    for record in trace.records:
        assert conform(m.code.entries["main"].region.job, record, {"x": 3}).ok
    return trace, unit, events


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("delay,expired", [(0.75, False), (1.0, True), (1.25, True)])
def test_dispatch_observation_at_deadline_expires_even_before_timers(core, delay, expired):
    trace, unit, events = execute(core, delay=delay)
    [record] = trace.records
    assert record.ok
    assert record.outputs["value"] == (-1 if expired else 4)
    assert unit.enqueued == [(0.0, 1.0)]
    if not expired:
        assert record.outputs["error"] is None
        assert "policy_failures" not in record.stats
        return
    error = record.outputs["error"]
    assert set(error) == {"kind", "node", "message"}
    assert error["kind"] == "ItemTimeout" and error["node"] == "inc"
    [failure] = record.stats["policy_failures"]
    assert {k: failure[k] for k in ("reason", "scope", "timeout_s", "source")} == {
        "reason": "deadline",
        "scope": "call",
        "timeout_s": 1.0,
        "source": "binding",
    }
    assert failure["record_index"] == 0 and failure["origin"] is not None
    assert len(record.record[0]) == 5 and "policy_failure" not in record.record[0][3]
    fault = [fields for kind, fields in events if kind == "fault.taken"]
    assert len(fault) == 1 and fault[0]["policy_failure"]["source"] == "binding"
    assert trace.clock_s == 1.0


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize(
    "call,attempt,scope,deadline",
    [
        (1.0, 0.25, "attempt", 0.25),
        (0.25, 1.0, "call", 0.25),
        (1.0, 1.0, "call", 1.0),
    ],
)
def test_earliest_call_or_attempt_deadline_wins_and_ties_choose_call(
    core, call, attempt, scope, deadline
):
    trace, _, _ = execute(core, delay=2.0, timeout=call, attempt=attempt)
    [record] = trace.records
    assert record.outputs["value"] == -1
    [failure] = record.stats["policy_failures"]
    assert failure["scope"] == scope
    assert failure["source"] == ("lane" if scope == "attempt" else "binding")
    assert failure["timeout_s"] == (attempt if scope == "attempt" else call)
    assert trace.clock_s == deadline


@pytest.mark.parametrize("core", ["python", "rust"])
def test_zero_cap_never_submits_and_queue_cap_covers_wait(core):
    trace, unit, _ = execute(core, delay=0, timeout=0)
    assert unit.enqueued == [] and trace.clock_s == 0
    assert trace.records[0].outputs["value"] == -1
    assert trace.records[0].stats["policy_failures"][0]["timeout_s"] == 0.0
    trace, unit, _ = execute(core, delay=2, timeout=1, capacity=0)
    assert unit.enqueued == [] and trace.clock_s == 1
    assert trace.records[0].stats["policy_failures"][0]["scope"] == "call"


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("backoff,submits", [(250, 2), (750, 1), (1000, 1)])
def test_retry_backoff_never_resets_original_call_cap(core, backoff, submits):
    unit = FixedDelay(0.25, unit_error=True, name="retrying")
    lane = Lane([unit], retry=Retry(max=5, backoff_ms=backoff))
    m = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        bind={"inc": Use(lane, policy=ExecutionPolicy(timeout_s=1))},
        core=core,
        record=1.0,
    )
    trace = run_trace(lambda: m, [{"x": 3}])
    [record] = trace.records
    assert record.ok and record.outputs["value"] == -1
    assert record.outputs["error"]["kind"] == "ItemTimeout"
    assert trace.clock_s == 1.0
    assert len(unit.enqueued) == submits
    assert all(deadline == 1.0 for _, deadline in unit.enqueued)
    assert record.stats["policy_failures"][0]["scope"] == "call"
    assert conform(m.code.entries["main"].region.job, record, {"x": 3}).ok


@pytest.mark.parametrize("core", ["python", "rust"])
def test_legacy_hint_override_empty_policy_and_host_timeout_distinguished(core):
    hinted = PROGRAM.replace("        (x)inc", "        @hint timeout_s=0.1\n        (x)inc")
    trace, _, _ = execute(core, source=hinted, policy=False, delay=1)
    assert trace.records[0].stats["policy_failures"][0]["source"] == "legacy_hint"
    trace, _, _ = execute(core, source=hinted, timeout=None, delay=1)
    assert trace.records[0].outputs == {"value": 4, "error": None}
    assert "policy_failures" not in trace.records[0].stats

    unit = FixedDelay(0, name="host")
    m = Machine(
        program=PROGRAM.replace("inc(value)", "host_timeout(value)"),
        entry="main",
        libraries=[LIB],
        bind={"host_timeout": Use(unit, policy=ExecutionPolicy(timeout_s=1))},
        core=core,
        record=1.0,
    )
    trace = run_trace(lambda: m, [{"x": 3}])
    [record] = trace.records
    assert record.outputs["error"]["kind"] == "ItemTimeout"
    assert "policy_failures" not in record.stats


@pytest.mark.parametrize("core", ["python", "rust"])
def test_store_hit_bypasses_call_cap_before_queue_preparation(core):
    store = MemoryStore()
    first, unit, _ = execute(core, timeout=None, delay=0.25, store=store, store_mode="content")
    assert first.records[0].outputs == {"value": 4, "error": None}
    second, unit, _ = execute(core, timeout=0, delay=2, store=store, store_mode="content")
    assert second.records[0].outputs == {"value": 4, "error": None}
    assert unit.enqueued == []
    assert "policy_failures" not in second.records[0].stats


@pytest.mark.parametrize("core", ["python", "rust"])
def test_policy_sidecar_survives_record_archive_without_changing_tuples(tmp_path, core):
    trace, _, _ = execute(
        core, delay=2, plugins=[Recorder(tmp_path, libraries=["test_execution_policy_runtime:LIB"])]
    )
    [record] = trace.records
    [archived] = Archive(tmp_path).jobs()
    assert archived["stats"]["policy_failures"] == record.stats["policy_failures"]
    assert all(len(call) == 5 for call in archived["record"])
    [replayed] = list(replay_dir(tmp_path))
    assert replayed.verdict.ok


class ReplaceItem(BasePlugin):
    name = "replace-policy-item"

    def __init__(self):
        self.items = []

    def pre_submit(self, item, unit, lane):
        fresh = WorkItem(
            item.node, item.inputs, item.adapter, protocol=item.protocol, deadline=99.0
        )
        fresh.ext.update(deadline_cap=99.0, call_timeout_s=99.0, policy_source="wrong")
        self.items.append((item, fresh))
        return fresh


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("retry", [False, True])
def test_replacement_preserves_single_deadline_authority_through_retry(core, retry):
    plugin = ReplaceItem()
    unit = FixedDelay(0.25, unit_error=retry, name="replace")
    lane = Lane([unit], retry=Retry(max=3, backoff_ms=2000))
    m = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        bind={"inc": Use(lane, policy=ExecutionPolicy(timeout_s=1))},
        core=core,
        record=1.0,
        plugins=[plugin],
    )
    trace = run_trace(lambda: m, [{"x": 3}])
    [record] = trace.records
    assert record.outputs["value"] == (-1 if retry else 4)
    assert len(plugin.items) == 1
    original, replacement = plugin.items[0]
    assert current(original) is replacement
    assert replacement.ext["deadline_cap"] == 1.0
    assert replacement.ext["policy_source"] == "binding"
    assert replacement.ext["settled"]
    assert lane.timers(10) == [] and lane.queued() == 0
    assert unit.counts["submits"] == 1
    assert len(record.stats.get("policy_failures", [])) == (1 if retry else 0)
    assert conform(m.code.entries["main"].region.job, record, {"x": 3}).ok


class PreparationTime(Fifo):
    def __init__(self, unit):
        self.unit = unit

    def priority(self, statement):
        clock = self.unit.schedule.clock
        clock.advance_to(clock() + 2)
        return 0


@pytest.mark.parametrize("core", ["python", "rust"])
def test_call_budget_starts_before_user_priority_preparation(core):
    unit = FixedDelay(0, name="preparation")
    m = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        bind={"inc": Use(unit, policy=ExecutionPolicy(timeout_s=1))},
        core=core,
        issue=PreparationTime(unit),
        record=1.0,
    )
    trace = run_trace(lambda: m, [{"x": 3}])
    [record] = trace.records
    assert record.outputs["value"] == -1 and record.outputs["error"]["kind"] == "ItemTimeout"
    assert unit.enqueued == []
    assert trace.clock_s == 2.0
    assert conform(m.code.entries["main"].region.job, record, {"x": 3}).ok


class SlowReplacement(ReplaceItem):
    def pre_submit(self, item, unit, lane):
        replacement = super().pre_submit(item, unit, lane)
        clock = unit.schedule.clock
        clock.advance_to(clock() + 2)
        return replacement


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("timeout,expired", [(2, True), (3, False)])
def test_presubmit_elapsed_time_rechecks_call_cap_and_starts_attempt_after_hook(
    core, timeout, expired
):
    plugin = SlowReplacement()
    unit = FixedDelay(0.25, name="slow-hook")
    lane = Lane([unit], timeout_s=0.5)
    m = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        bind={"inc": Use(lane, policy=ExecutionPolicy(timeout_s=timeout))},
        core=core,
        record=1.0,
        plugins=[plugin],
    )
    trace = run_trace(lambda: m, [{"x": 3}])
    [record] = trace.records
    assert record.outputs["value"] == (-1 if expired else 4)
    if expired:
        assert unit.enqueued == [] and trace.clock_s == 2
        [failure] = record.stats["policy_failures"]
        assert failure["scope"] == "call" and failure["source"] == "binding"
        assert failure["timeout_s"] == 2.0
    else:
        assert unit.enqueued == [(2.0, 2.5)] and trace.clock_s == 2.25
        assert record.outputs["error"] is None and "policy_failures" not in record.stats
    assert lane.timers(10) == [] and lane.queued() == 0
    assert conform(m.code.entries["main"].region.job, record, {"x": 3}).ok


def test_replacement_forwarding_compresses_and_reuse_does_not_cycle():
    original = WorkItem("inc", {}, None, deadline=1)
    original.ext.update(deadline_cap=1, call_timeout_s=1, policy_source="binding")
    middle = WorkItem("inc", {}, None)
    last = WorkItem("inc", {}, None)
    replace(original, middle)
    replace(middle, last)
    assert current(original) is last
    assert original.ext["_deadline_replacement"] is last
    replace(last, original)
    assert current(middle) is original and current(original) is original
    assert original.ext["deadline_cap"] == 1
