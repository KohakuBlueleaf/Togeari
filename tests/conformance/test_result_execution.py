"""Execution-compatible v2 result caches and local dynamic-history guards."""

import copy
import logging
import pickle

import pytest
from kohakunode import NodeLibrary

from togeari import BasePlugin, Lane, Machine, Use
from togeari.core.contracts.digest import digest
from togeari.core.lanes.lane import Retry
from togeari.core.program import execution as execution_module
from togeari.core.program.execution import current_execution
from togeari.core.runtime.backend.persistence import result_execution
from togeari.policies import Fixed, SizeOrWait
from togeari.stores import MemoryStore
from togeari.testing.trace import ScriptedEngine, run_trace

PROGRAM = "@def (x: int)main(y):\n    (x)inc(y)\n"


class FixedEngine(ScriptedEngine):
    def delay(self):
        return 0.25


class CountingStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.writes = []

    def get(self, key):
        self.reads.append(key)
        return super().get(key)

    def put(self, key, value):
        self.writes.append(key)
        super().put(key, value)


def build(
    core,
    store,
    *,
    compatibility="strict",
    mode="content",
    revision="implementation/1",
    bundle="binding/1",
    delta=1,
    workers=1,
    capacity=8,
    batch=False,
    plugins=(),
    record=1.0,
):
    lib = NodeLibrary("execution_cache")

    @lib.node()
    def inc(x: int) -> int:
        return x + delta

    units = [
        FixedEngine(
            "batch" if batch else "call", name=f"unit{i}", capacity=capacity, max_batch=capacity
        )
        for i in range(workers)
    ]
    lane = Lane(units, former=SizeOrWait(capacity, 0) if batch else None)
    machine = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[lib],
        bind={
            "inc": Use(lane, store=mode, implementation_revision=revision, binding_revision=bundle)
        },
        core=core,
        store=store,
        record=record,
        persistence_compatibility=compatibility,
        plugins=list(plugins),
        admission=Fixed(1),
    )
    return machine, lane


def execute(core, store, **kwargs):
    machine, lane = build(core, store, **kwargs)
    trace = run_trace(lambda: machine, [{"x": 3}])
    [record] = trace.records
    assert record.ok, record.failure
    return machine, lane, record


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("mode", ["content", "positional"])
def test_versioned_result_keys_include_execution_revision_and_ignore_old_keys(core, mode):
    store = CountingStore()
    first, _, record = execute(core, store, mode=mode)
    assert record.outputs == {"y": 4}
    execution = record.stats["execution"]["hash"]
    region = first.code.entries["main"].region
    if mode == "content":
        expected = digest(("togeari.result", 2, "content", execution, "inc", digest({"x": 3})))
    else:
        invocation = record.stats["record_invocations"][0]["invocation_id"]
        expected = digest(("togeari.result", 3, "positional", execution, invocation))
    assert store.writes == [expected]
    reads = len(store.reads)
    second, lane, reused = execute(core, store, mode=mode)
    assert reused.outputs == {"y": 4}
    assert len(store.reads) == reads + 1
    if mode == "content":
        assert sum(u.counts["submits"] for u in lane.units) == 0
        assert store.reads[-1] == expected
    else:
        assert sum(u.counts["submits"] for u in lane.units) == 1
        next_invocation = reused.stats["record_invocations"][0]["invocation_id"]
        assert next_invocation != invocation
        assert store.reads[-1] == digest(
            ("togeari.result", 3, "positional", execution, next_invocation)
        )
        assert store.reads[-1] != expected
    _, lane, changed = execute(core, store, mode=mode, revision="implementation/2", delta=2)
    assert changed.outputs == {"y": 5} and sum(u.counts["submits"] for u in lane.units) == 1
    assert changed.stats["execution"]["hash"] != execution
    assert record.stats["persistence"]["status"] == "compatible"

    # Legacy domains are never probed, even when populated with a plausible wrong result.
    stale = CountingStore()
    if mode == "content":
        [ins] = list(first.code.calls(region))
        legacy = digest(
            (
                region.semantic_hash,
                "inc",
                digest({"x": 3}),
                first.identity(ins.binding.adapter, ins.binding.lane),
            )
        )
    else:
        legacy = digest((region.semantic_hash, "0", digest({"x": 3}), 1, (0,)))
        [ins] = list(first.code.calls(region))
        previous_version = digest(
            (
                "togeari.result",
                2,
                "positional",
                execution,
                "0",
                digest({"x": 3}),
                ins.addr - region.start,
                (0,),
            )
        )
        stale.put(previous_version, 901)
    stale.put(legacy, 900)
    _, _, fresh = execute(core, stale, mode=mode)
    assert fresh.outputs == {"y": 4} and legacy not in stale.reads
    if mode == "positional":
        assert previous_version not in stale.reads


@pytest.mark.parametrize("core", ["python", "rust"])
def test_performance_only_replica_and_batch_changes_do_not_change_namespace(core):
    store = CountingStore()
    _, _, first = execute(core, store, workers=1, capacity=1)
    _, lane, second = execute(core, store, workers=3, capacity=8, batch=True)
    assert first.stats["execution"]["hash"] == second.stats["execution"]["hash"]
    assert second.outputs == {"y": 4}
    assert sum(unit.counts["submits"] for unit in lane.units) == 0


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("compatibility,allowed", [("warn", True), ("strict", False)])
def test_incomplete_revision_is_visible_warn_reuse_or_strict_miss(
    core, compatibility, allowed, caplog
):
    store = CountingStore()
    # Other modules disable logging at collection; caplog restores that global state on exit.
    with caplog.at_level(logging.WARNING, logger="togeari.persistence"):
        _, _, first = execute(core, store, compatibility=compatibility, revision=None, bundle=None)
        _, lane, second = execute(
            core, store, compatibility=compatibility, revision=None, bundle=None
        )
    assert first.outputs == second.outputs == {"y": 4}
    assert second.stats["persistence"]["status"] == "unverified"
    assert second.stats["persistence"]["allow"] == allowed
    assert bool(store.reads) == allowed and bool(store.writes) == allowed
    assert sum(unit.counts["submits"] for unit in lane.units) == (0 if allowed else 1)
    assert "unverified" in caplog.text if allowed else "result persistence skipped" in caplog.text


class ChangeDuringCall(BasePlugin):
    name = "change-during-call"

    def __init__(self, kind):
        self.kind = kind
        self.changed = False

    def on_load(self, ctx):
        self.lane = next(ins.binding.lane for ins in ctx.code.mem if ins.binding is not None)

    def pre_submit(self, item, unit, lane):
        lane = self.lane
        if not self.changed:
            self.changed = True
            match self.kind:
                case "timeout":
                    lane.timeout_s = 9
                    lane.timeout_s = None
                case "retry":
                    lane.retry.max += 1
                    lane.retry.max -= 1
                case "membership":
                    new = FixedEngine("call", name="new")
                    new.bind(unit.schedule, 0)
                    lane.attach(new)
        return item


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("kind", ["timeout", "retry", "membership"])
@pytest.mark.parametrize("compatibility", ["warn", "strict"])
@pytest.mark.parametrize("mode", ["content", "positional"])
def test_mixed_active_history_cannot_persist_even_after_policy_reverts(
    core, kind, compatibility, mode
):
    store = CountingStore()
    plugin = ChangeDuringCall(kind)
    machine, lane, record = execute(
        core, store, plugins=[plugin], compatibility=compatibility, mode=mode
    )
    assert record.outputs == {"y": 4}
    assert store.writes == []
    assert record.stats["persistence"]["allow"] is False
    assert record.stats["persistence"]["issues"][0]["code"] == "unsupported_history"
    assert machine._execution_epochs[0] > record.stats["execution_epoch"]
    current = current_execution(machine.code.regions[0], machine.code, machine)
    assert current["completeness"]["transitions"] == []
    assert current["completeness"]["complete"] == (kind != "membership")


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("kind", ["timeout", "membership"])
@pytest.mark.parametrize("compatibility", ["warn", "strict"])
def test_future_jobs_use_new_admission_epoch_not_old_mixed_history(core, kind, compatibility):
    store = CountingStore()
    machine, lane = build(
        core, store, compatibility=compatibility, plugins=[ChangeDuringCall(kind)]
    )
    trace = run_trace(lambda: machine, [{"x": 3}] * 3)
    first, second, third = trace.records
    assert all(record.ok and record.outputs == {"y": 4} for record in trace.records)
    assert first.stats["persistence"]["issues"][0]["code"] == "unsupported_history"
    assert (
        second.stats["execution_epoch"]
        == third.stats["execution_epoch"]
        > first.stats["execution_epoch"]
    )
    assert second.stats["execution"]["completeness"]["transitions"] == []
    allowed = kind == "timeout" or compatibility == "warn"
    assert second.stats["persistence"]["allow"] == allowed
    assert third.stats["persistence"]["allow"] == allowed
    assert len(store.writes) == (1 if allowed else 0)
    assert sum(unit.counts["submits"] for unit in lane.units) == (2 if allowed else 3)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_policy_hooks_track_before_and_after_without_false_performance_transitions(core):
    machine, lane = build(core, CountingStore())
    try:
        region = machine.code.regions[0]
        initial = current_execution(region, machine.code, machine)
        start = machine._execution_epochs[region.index]
        lane.timeout_s = 5
        lane.timeout_s = None
        lane.retry.backoff_ms = 300
        lane.retry.backoff_ms = 200
        lane.retry = Retry(4, 200)
        lane.retry = Retry(3, 200)
        lane.lane_dead_s = 100
        lane.lane_dead_s = 300
        assert machine._execution_epochs[region.index] == start + 8
        current = current_execution(region, machine.code, machine)
        assert current["hash"] == initial["hash"]
        before = machine._execution_epochs[region.index]
        lane.max_inflight = 9
        lane.units[0].cap = 19
        lane.units[0].max_batch = 19
        assert current_execution(region, machine.code, machine)["hash"] == initial["hash"]
        assert machine._execution_epochs[region.index] == before
        copied = copy.deepcopy(lane.retry)
        restored = pickle.loads(pickle.dumps(lane.retry))
        assert copied == restored == lane.retry
        copied.max += 10
        restored.backoff_ms += 10
        assert machine._execution_epochs[region.index] == before
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_result_guards_cache_self_validation_for_unchanged_execution(core, monkeypatch):
    checked = []
    original = execution_module.compatibility

    def counted(saved, current, mode):
        checked.append((saved is current, mode))
        return original(saved, current, mode)

    monkeypatch.setattr(execution_module, "compatibility", counted)
    store = CountingStore()
    machine, lane = build(core, store, record=None)
    trace = run_trace(lambda: machine, [{"x": 3}] * 10)
    assert all(record.ok and record.outputs == {"y": 4} for record in trace.records)
    assert sum(unit.counts["submits"] for unit in lane.units) == 1
    assert checked == [(True, "warn"), (True, "strict")]


def test_result_decision_copy_is_reused_but_missing_admission_is_a_diagnosed_miss():
    machine, _ = build("python", CountingStore(), compatibility="warn", revision=None, bundle=None)
    try:
        region = machine.code.regions[0]
        current = current_execution(region, machine.code, machine)
        stats = {"execution": copy.deepcopy(current), "execution_epoch": current["epoch"]}
        assert result_execution(region, machine.code, machine, stats) == current["hash"]
        decision = stats["persistence"]
        assert decision is not region.execution_decisions["warn"]
        assert decision["issues"] is not region.execution_decisions["warn"]["issues"]
        for _ in range(20):
            assert result_execution(region, machine.code, machine, stats) == current["hash"]
            assert stats["persistence"] is decision
        missing = {}
        assert result_execution(region, machine.code, machine, missing) is None
        assert missing["persistence"]["status"] == "invalid"
    finally:
        machine.stop()


class ChangingStore(CountingStore):
    def __init__(self):
        super().__init__()
        self.lane = None

    def get(self, key):
        value = super().get(key)
        if self.lane is not None:
            self.lane.timeout_s = 5
            self.lane.timeout_s = None
        return value


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("mode", ["content", "positional"])
@pytest.mark.parametrize("compatibility", ["warn", "strict"])
def test_store_get_callback_transition_discards_hit_before_consumption(core, mode, compatibility):
    store = ChangingStore()
    execute(core, store, mode=mode, compatibility=compatibility)
    writes = len(store.writes)
    machine, lane = build(core, store, mode=mode, compatibility=compatibility)
    store.lane = lane
    trace = run_trace(lambda: machine, [{"x": 3}])
    [record] = trace.records
    assert record.ok and record.outputs == {"y": 4}
    assert sum(unit.counts["submits"] for unit in lane.units) == 1
    assert record.stats["persistence"]["issues"][0]["code"] == "unsupported_history"
    assert len(store.writes) == writes
    assert machine.dispatcher.stats["store_hits"] == 0


class ChangingDigest:
    def __init__(self, lane):
        self.lane = lane
        self.calls = 0

    def __togeari_digest__(self):
        self.calls += 1
        if self.calls == 2:  # admission input digest, then content-key digest
            self.lane.retry.max += 1
            self.lane.retry.max -= 1
        return b"constant-input-contract"

    def __add__(self, other):
        return 3 + other


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("compatibility", ["warn", "strict"])
def test_digest_callback_transition_denies_lookup_after_key_build(core, compatibility):
    store = CountingStore()
    machine, lane = build(core, store, compatibility=compatibility, record=None)
    value = ChangingDigest(lane)
    [record] = run_trace(lambda: machine, [{"x": value}]).records
    assert record.ok and record.outputs == {"y": 4}
    assert value.calls >= 2
    assert store.reads == store.writes == []
    assert record.stats["persistence"]["issues"][0]["code"] == "unsupported_history"


class DrainDuringCall(BasePlugin):
    name = "drain-during-call"

    def on_load(self, ctx):
        self.lane = next(ins.binding.lane for ins in ctx.code.mem if ins.binding is not None)
        self.scheduled = False

    def pre_submit(self, item, unit, lane):
        if not self.scheduled:
            self.scheduled = True
            unit.schedule.at(
                unit.schedule.clock() + 0.125,
                lambda: self.lane.detach(unit, True, unit.schedule.clock()),
            )
        return item


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("compatibility", ["warn", "strict"])
def test_draining_unit_is_execution_transition_before_inflight_result_persists(core, compatibility):
    store = CountingStore()
    machine, _, record = execute(
        core, store, compatibility=compatibility, plugins=[DrainDuringCall()]
    )
    assert record.ok and record.outputs == {"y": 4}
    assert store.writes == []
    assert record.stats["persistence"]["issues"][0]["code"] == "unsupported_history"
    assert machine._execution_epochs[0] > record.stats["execution_epoch"]
