"""Logical invocation identity, actual attempts, plugin transport and reuse resolution."""

import re

import pytest
from kohakunode import NodeLibrary

from togeari import BasePlugin, ExecutionPolicy, Inline, Lane, Machine, SourceItem, Use
from togeari.conformance import conform
from togeari.core.contracts.identity import invocation_id, new_job_execution_id
from togeari.core.contracts.results import Ok, UnitError
from togeari.core.contracts.workitem import WorkItem
from togeari.core.lanes.lane import Retry
from togeari.core.plugin.base import PluginBlockError
from togeari.core.runtime.identity import record_invocation
from togeari.policies import SizeOrWait
from togeari.stores import MemoryStore
from togeari.testing.trace import ScriptedEngine, run_trace

LIB = NodeLibrary("invocation_test")


@LIB.node()
def inc(x: int) -> int:
    return x + 1


ONE = "@def (x: int)main(y):\n    (x)inc(y)\n"
TWO = "@def (x: int)main(a, b):\n    (x)inc(a)\n    (x)inc(b)\n"
CATCH = """@def (x: int)main(y, error):
    y = -1
    error = None
    @try:
        (x)inc(y)
    @except caught:
        error = caught
"""
HEX32 = re.compile("[0-9a-f]{32}\\Z")
HEX64 = re.compile("[0-9a-f]{64}\\Z")


class IdentityEngine(ScriptedEngine):
    def __init__(self, *, failures=0, raises=False, kind="call", **kwargs):
        super().__init__(kind, **kwargs)
        self.failures = failures
        self.raises = raises
        self.identities = []

    def delay(self):
        return 0.125

    def submit(self, work, done):
        for item in work if isinstance(work, list) else [work]:
            self.identities.append((item.job_execution_id, item.invocation_id, item.attempt_id))
            assert not any(
                name in item.ext
                for name in ("job_execution_id", "invocation_id", "attempt_id", "attempt_ids")
            )
        if self.raises and len(self.identities) == 1:
            raise ConnectionError("submit raised")
        return super().submit(work, done)

    def run_one(self, item, fail):
        if self.failures:
            self.failures -= 1
            return UnitError(ConnectionError("retry this item"))
        return super().run_one(item, fail)


def factory(
    core,
    unit,
    *,
    program=ONE,
    plugins=(),
    record=1.0,
    store=None,
    mode=None,
    deterministic=False,
    timeout=None,
    retry=None,
):
    lane = Lane([unit], retry=retry)
    return Machine(
        program=program,
        entry="main",
        libraries=[LIB],
        core=core,
        bind={
            "inc": Use(
                lane,
                store=mode,
                deterministic=deterministic,
                implementation_revision="inc/1",
                binding_revision="unit/1",
                policy=ExecutionPolicy(timeout_s=timeout),
            )
        },
        record=record,
        store=store,
        plugins=list(plugins),
    )


def sidecar(record):
    assert record.stats["identity_version"] == 1
    assert HEX32.fullmatch(record.stats["job_execution_id"])
    records = record.stats["record_invocations"]
    assert len(records) == len(record.record)
    for entry in records:
        assert HEX64.fullmatch(entry["invocation_id"])
        assert all(HEX32.fullmatch(attempt) for attempt in entry["attempt_ids"])
    return records


@pytest.mark.parametrize("core", ["python", "rust"])
def test_equal_inputs_distinct_calls_and_fresh_runs_have_distinct_ids(core):
    runs = []
    for _ in range(2):
        unit = IdentityEngine(name="identity")
        machine = factory(core, unit, program=TWO)
        [record] = run_trace(
            lambda machine=machine: machine, [SourceItem("same", {"x": 3})]
        ).records
        assert record.ok and record.outputs == {"a": 4, "b": 4}
        entries = sidecar(record)
        assert len({entry["invocation_id"] for entry in entries}) == 2
        assert len({ids[2] for ids in unit.identities}) == 2
        assert all(ids[0] == record.stats["job_execution_id"] for ids in unit.identities)
        assert {ids[1] for ids in unit.identities} == {entry["invocation_id"] for entry in entries}
        assert all(
            entry["resolution"] == "executed" and len(entry["attempt_ids"]) == 1
            for entry in entries
        )
        assert conform(machine.code.entries["main"].region.job, record, {"x": 3}).ok
        runs.append(record)
    assert runs[0].stats["job_execution_id"] != runs[1].stats["job_execution_id"]
    assert {entry["invocation_id"] for entry in sidecar(runs[0])}.isdisjoint(
        entry["invocation_id"] for entry in sidecar(runs[1])
    )


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("raises", [False, True])
def test_retry_retains_invocation_and_adds_actual_submission_attempt(core, raises):
    unit = IdentityEngine(name="retry", failures=0 if raises else 1, raises=raises)
    machine = factory(core, unit, retry=Retry(max=2, backoff_ms=0))
    [record] = run_trace(lambda machine=machine: machine, [{"x": 3}]).records
    assert record.ok and record.outputs == {"y": 4}
    [entry] = sidecar(record)
    assert len(unit.identities) == 2
    assert len({ids[1] for ids in unit.identities}) == 1
    assert len({ids[2] for ids in unit.identities}) == 2
    assert entry["attempt_ids"] == [ids[2] for ids in unit.identities]
    assert entry["resolution"] == "executed"


class LateAttemptEngine(IdentityEngine):
    def __init__(self, stale_error):
        super().__init__(name="late-attempt", failures=1)
        self.stale_error = stale_error
        self.old_done = None
        self.old_ticket = None
        self.stale_sent = 0

    def submit(self, work, done):
        if self.old_done is not None:
            stale = (
                UnitError(ConnectionError("obsolete unit failure"))
                if self.stale_error
                else Ok(999, True)
            )
            self.old_done(self.old_ticket, [stale])
            self.stale_sent += 1
        ticket = super().submit(work, done)
        if self.old_done is None:
            self.old_done, self.old_ticket = done, ticket
        return ticket


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("stale_error", [False, True])
def test_stale_previous_attempt_callback_cannot_settle_new_attempt(core, stale_error):
    unit = LateAttemptEngine(stale_error)
    machine = factory(core, unit, retry=Retry(max=3, backoff_ms=0))
    [record] = run_trace(lambda: machine, [{"x": 3}]).records
    assert record.ok and record.outputs == {"y": 4}
    assert len(unit.identities) == 2 and unit.stale_sent == 1
    [entry] = sidecar(record)
    assert len({ids[1] for ids in unit.identities}) == 1
    assert entry["invocation_id"] == unit.identities[0][1]
    assert entry["attempt_ids"] == [ids[2] for ids in unit.identities]
    assert len(set(entry["attempt_ids"])) == 2 and entry["resolution"] == "executed"
    assert machine.lanes[0].stats["retries"] == 1
    assert "policy_failures" not in record.stats
    assert conform(machine.code.entries["main"].region.job, record, {"x": 3}).ok


class SpoofIdentity(BasePlugin):
    name = "spoof-identity"

    def __init__(self, replacement=False, blocked=False, expire=False):
        self.replacement = replacement
        self.blocked = blocked
        self.expire = expire
        self.seen = []

    def pre_submit(self, item, unit, lane):
        self.seen.append((item.job_execution_id, item.invocation_id, item.attempt_id))
        item._set_identity("a" * 32, "b" * 64, "c" * 32)
        item.ext.update(
            job_execution_id="fake", invocation_id="fake", attempt_id="fake", attempt_ids=["fake"]
        )
        item.owner = object()
        if self.expire:
            unit.schedule.clock.advance_to(2)
        if self.blocked:
            raise PluginBlockError("blocked after spoof")
        if self.replacement:
            new = WorkItem(
                item.node,
                item.inputs,
                item.adapter,
                owner=object(),
                job_execution_id="d" * 32,
                invocation_id="e" * 64,
                attempt_id="f" * 32,
            )
            new.ext["invocation_id"] = "spoof"
            return new
        return item


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("replacement", [False, True])
def test_plugin_cannot_replace_authoritative_ids_or_owner(core, replacement):
    plugin = SpoofIdentity(replacement=replacement)
    unit = IdentityEngine(name="protected")
    machine = factory(core, unit, plugins=[plugin])
    [record] = run_trace(lambda machine=machine: machine, [{"x": 3}]).records
    assert record.ok and record.outputs == {"y": 4}
    [entry] = sidecar(record)
    [(job_id, invocation, attempt)] = unit.identities
    assert (job_id, invocation) == (record.stats["job_execution_id"], entry["invocation_id"])
    assert entry["attempt_ids"] == [attempt] and attempt != "c" * 32
    assert plugin.seen == [(job_id, invocation, None)]


class SpoofRetryOnly(SpoofIdentity):
    def pre_submit(self, item, unit, lane):
        if not unit.identities:
            return item
        return super().pre_submit(item, unit, lane)


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("expire", [False, True])
def test_retry_block_or_posthook_expiry_keeps_first_actual_attempt(core, expire):
    plugin = SpoofRetryOnly(replacement=True, blocked=not expire, expire=expire)
    unit = IdentityEngine(name="one-actual-attempt", failures=1)
    machine = factory(
        core, unit, program=CATCH, plugins=[plugin], timeout=1, retry=Retry(max=3, backoff_ms=0)
    )
    [record] = run_trace(lambda: machine, [{"x": 3}]).records
    assert record.ok and record.outputs["y"] == -1
    assert record.outputs["error"]["kind"] == ("ItemTimeout" if expire else "Blocked")
    assert len(unit.identities) == 1
    [entry] = sidecar(record)
    job, invocation, first_attempt = unit.identities[0]
    assert plugin.seen == [(job, invocation, first_attempt)]
    assert entry == {
        "invocation_id": invocation,
        "attempt_ids": [first_attempt],
        "resolution": "executed",
    }
    assert conform(machine.code.entries["main"].region.job, record, {"x": 3}).ok


class HostileItem(WorkItem):
    def _set_identity(self, *fields):
        self._job_execution_id = "a" * 32
        self._invocation_id = "b" * 64
        self._attempt_id = "c" * 32

    def _begin_attempt(self):
        return "c" * 32


class ReplaceWithSubclass(BasePlugin):
    name = "replace-subclass"

    def pre_submit(self, item, unit, lane):
        item.owner = object()
        item._set_identity("d" * 32, "e" * 64, "f" * 32)
        return HostileItem(item.node, item.inputs, item.adapter)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_machine_blocks_workitem_subclass_overriding_identity_transport(core):
    unit = IdentityEngine(name="subclass-blocked")
    machine = factory(core, unit, program=CATCH, plugins=[ReplaceWithSubclass()])
    [record] = run_trace(lambda: machine, [{"x": 3}]).records
    assert record.ok and record.outputs["y"] == -1
    assert record.outputs["error"]["kind"] == "Blocked"
    assert "exact WorkItem" in record.outputs["error"]["message"]
    assert unit.identities == []
    [entry] = sidecar(record)
    assert entry["resolution"] == "not_submitted" and entry["attempt_ids"] == []
    assert entry["invocation_id"] != "e" * 64
    assert conform(machine.code.entries["main"].region.job, record, {"x": 3}).ok


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("case", ["zero", "queue", "blocked", "posthook"])
def test_no_actual_submission_means_no_attempt(core, case):
    plugin = SpoofIdentity(blocked=case == "blocked", expire=case == "posthook")
    unit = IdentityEngine(name="never", capacity=0 if case == "queue" else 8)
    timeout = 0 if case == "zero" else 1
    machine = factory(
        core,
        unit,
        program=CATCH,
        timeout=timeout,
        plugins=[plugin] if case in ("blocked", "posthook") else [],
    )
    [record] = run_trace(lambda machine=machine: machine, [{"x": 3}]).records
    assert record.ok and record.outputs["y"] == -1
    assert unit.identities == []
    [entry] = sidecar(record)
    assert entry["attempt_ids"] == [] and entry["resolution"] == "not_submitted"
    assert record.outputs["error"]["kind"] == ("Blocked" if case == "blocked" else "ItemTimeout")


@pytest.mark.parametrize("core", ["python", "rust"])
def test_content_hit_has_own_invocation_but_no_attempt(core):
    store = MemoryStore()
    records = []
    for _ in range(2):
        unit = IdentityEngine(name="cache")
        machine = factory(core, unit, store=store, mode="content", deterministic=True)
        [record] = run_trace(
            lambda machine=machine: machine, [SourceItem("same", {"x": 3})]
        ).records
        assert record.ok and record.outputs == {"y": 4}
        records.append((record, unit.identities))
    [first] = sidecar(records[0][0])
    [second] = sidecar(records[1][0])
    assert first["resolution"] == "executed" and len(first["attempt_ids"]) == 1
    assert second["resolution"] == "content_hit" and second["attempt_ids"] == []
    assert second["invocation_id"] != first["invocation_id"] and records[1][1] == []


@pytest.mark.parametrize("core", ["python", "rust"])
def test_ids_exist_when_recording_disabled_and_record_limit_clears_sidecar(core):
    for recording, limit in ((None, None), (1.0, 1)):
        unit = IdentityEngine(name="unrecorded")
        machine = factory(core, unit, program=TWO, record=recording)
        machine.knobs["record_limit"] = limit
        [record] = run_trace(lambda machine=machine: machine, [{"x": 3}]).records
        assert record.ok and record.record is None
        assert len(unit.identities) == 2
        assert all(
            HEX32.fullmatch(job) and HEX64.fullmatch(inv) and HEX32.fullmatch(attempt)
            for job, inv, attempt in unit.identities
        )
        assert "record_invocations" not in record.stats
        assert "record_origins" not in record.stats


@pytest.mark.parametrize("core", ["python", "rust"])
def test_iterations_and_batch_members_have_distinct_invocations_and_attempts(core):
    text = """@def (xs: list)main(out):
    (xs)collect(x, `body`, y -> out)
    body:
        (x)inc(y)
"""
    unit = IdentityEngine(name="batch", kind="batch", capacity=2, max_batch=4)
    lane = Lane([unit], former=SizeOrWait(4, 0))
    machine = Machine(
        program=text,
        entry="main",
        libraries=[LIB],
        core=core,
        record=1.0,
        bind={"inc": Use(lane, deterministic=False)},
    )
    [record] = run_trace(lambda machine=machine: machine, [{"xs": [3, 3, 3, 3]}]).records
    assert record.ok and record.outputs == {"out": [4, 4, 4, 4]}
    entries = sidecar(record)
    assert len(entries) == 4
    assert len({entry["invocation_id"] for entry in entries}) == 4
    assert len({attempt for entry in entries for attempt in entry["attempt_ids"]}) == 4
    assert {entry["invocation_id"] for entry in entries} == {ids[1] for ids in unit.identities}
    assert conform(machine.code.entries["main"].region.job, record, {"xs": [3, 3, 3, 3]}).ok


@pytest.mark.parametrize("core", ["python", "rust"])
def test_inline_calls_allocate_attempts_without_workitems(core):
    machine = Machine(
        program=TWO,
        entry="main",
        libraries=[LIB],
        core=core,
        record=1.0,
        bind={"inc": Inline(deterministic=False)},
    )
    [record] = run_trace(lambda machine=machine: machine, [{"x": 3}]).records
    assert record.ok
    entries = sidecar(record)
    assert len({entry["invocation_id"] for entry in entries}) == 2
    assert all(
        entry["resolution"] == "executed" and len(entry["attempt_ids"]) == 1 for entry in entries
    )


def test_workitem_readonly_identity_and_legacy_sidecar_padding():
    job = new_job_execution_id()
    inv = invocation_id(job, "a" * 64, "b" * 64, 0, (1,))
    item = WorkItem("raw", {}, None, job_execution_id=job, invocation_id=inv)
    with pytest.raises(AttributeError):
        item.invocation_id = "c" * 64
    first, second = item._begin_attempt(), item._begin_attempt()
    assert first != second and HEX32.fullmatch(first) and HEX32.fullmatch(second)
    assert item.job_execution_id == job and item.invocation_id == inv
    rows = []
    record_invocation(rows, 2, inv, [first, second], "executed")
    assert rows[:2] == [None, None]
    assert rows[2] == {
        "invocation_id": inv,
        "attempt_ids": [first, second],
        "resolution": "executed",
    }
