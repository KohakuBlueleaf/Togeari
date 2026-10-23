"""Logical calls and actual inline attempts exist independently of tracing and cache use."""

import copy

import pytest
from kohakunode import NodeLibrary

from togeari import BasePlugin, Inline, Lane, Machine, Use
from togeari.core.contracts import identity as identity_contract
from togeari.core.contracts.digest import digest
from togeari.core.contracts.identity import (
    invocation_id,
    validate_attempt_id,
    validate_invocation_id,
    validate_job_execution_id,
)
from togeari.core.program.decode.instr import Op
from togeari.core.program.execution import observe_execution
from togeari.core.runtime import state as runtime_state
from togeari.core.runtime.state import Inflight, InfState, Job
from togeari.testing import SimCall


@pytest.mark.parametrize("record", [0, 1.0])
@pytest.mark.parametrize("core", ["python", "rust"])
def test_inline_attempt_exists_before_function_and_fresh_runs_get_new_identity(
    record, core, monkeypatch
):
    library = NodeLibrary("inline_identity")
    events, observed = [], []
    new_attempt = identity_contract.new_attempt_id

    def tracked_attempt():
        attempt = new_attempt()
        events.append(("attempt", attempt))
        return attempt

    # Native caches the factory at Machine construction; no callback reenters its
    # live core or assumes invocation hashing calls into Python.
    monkeypatch.setattr(identity_contract, "new_attempt_id", tracked_attempt)
    monkeypatch.setattr(runtime_state, "new_attempt_id", tracked_attempt)

    @library.node(outputs=["y"])
    def sample(x: int) -> int:
        kind, attempt = events[-1]
        assert kind == "attempt"
        validate_attempt_id(attempt)
        observed.append(attempt)
        events.append(("function", attempt))
        return x

    machine = Machine(
        program="@def (x: int)main(y):\n    (x)sample(y)\n",
        entry="main",
        libraries=[library],
        bind={"*": Inline()},
        record=record,
        core=core,
    )
    try:
        first = machine.submit({"x": 7}, key="same").result(5)
        second = machine.submit({"x": 7}, key="same").result(5)
        assert first.ok and second.ok and first.outputs == second.outputs == {"y": 7}
        assert len(observed) == len(set(observed)) == 2
        assert first.stats["job_execution_id"] != second.stats["job_execution_id"]
        region = machine.code.entries["main"].region
        instruction = next(machine.code.calls(region))
        for result, attempt in zip((first, second), observed):
            uid = validate_job_execution_id(result.stats["job_execution_id"])
            assert result.stats["identity_version"] == 1
            if record:
                expected = invocation_id(
                    uid,
                    region.semantic_hash,
                    region.execution["hash"],
                    instruction.addr - region.start,
                    (0,),
                )
                validate_invocation_id(expected)
                assert result.stats["record_invocations"] == [
                    {
                        "invocation_id": expected,
                        "attempt_ids": [attempt],
                        "resolution": "executed",
                    }
                ]
                assert all(len(call) == 5 for call in result.record)
            else:
                assert result.record is None and "record_invocations" not in result.stats
    finally:
        machine.stop()


class CorruptDiagnosticIdentity(BasePlugin):
    machine = None

    def on_job_start(self, info):
        job = next(iter(self.machine.dispatcher.jobs.values()))
        job.stats["execution"]["hash"] = "0" * 64
        job.stats["program"] = "1" * 64


@pytest.mark.parametrize("core", ["python", "rust"])
def test_invocation_uses_authoritative_admission_hashes_not_plugin_visible_stats(core):
    plugin = CorruptDiagnosticIdentity()
    machine = Machine(
        program="@def ()main(y):\n    (7)identity(y)\n",
        entry="main",
        bind={"*": Inline()},
        plugins=[plugin],
        record=1.0,
        core=core,
    )
    plugin.machine = machine
    try:
        region = machine.code.entries["main"].region
        execution_hash = region.execution["hash"]
        instruction = next(machine.code.calls(region))
        record = machine.submit({}, key="stats-edit").result(5)
        assert record.ok and record.outputs == {"y": 7}
        assert record.stats["execution"]["hash"] == "0" * 64
        [sidecar] = record.stats["record_invocations"]
        assert sidecar["invocation_id"] == invocation_id(
            record.stats["job_execution_id"],
            region.semantic_hash,
            execution_hash,
            instruction.addr - region.start,
            (0,),
        )
    finally:
        machine.stop()


@pytest.mark.parametrize("change", ["none", "retry", "membership"])
def test_deterministic_memo_requires_unchanged_history_not_persistence_completeness(change):
    library = NodeLibrary("memo_identity")
    library.add("work", lambda x: x * 2, outputs=["y"])
    lane = Lane([SimCall(name="worker")])
    machine = Machine(
        program="@def (x)main(y):\n    (x)work(y)\n",
        entry="main",
        libraries=[library],
        bind={"work": Use(lane, deterministic=True)},
        persistence_compatibility="strict",
        core="python",
        start_units=False,
    )
    try:
        entry = machine.code.entries["main"]
        job = Job("memo", entry, {"x": 1})
        observation = observe_execution(entry.region, machine.code, machine)
        assert not observation["manifest"]["completeness"]["complete"]
        job.stats.update(
            execution=copy.deepcopy(observation["manifest"]), execution_epoch=observation["epoch"]
        )
        instruction = next(machine.code.calls(entry.region))
        completed = Inflight(job, instruction, (0,))
        completed.values, completed.result, completed.state = {"x": 1}, 2, InfState.DONE
        machine.dispatcher.keep_for_reuse(completed, 10, None)
        assert job.memo is not None and job.memo.n == 1
        if change == "retry":
            lane.retry.max += 1
        elif change == "membership":
            lane.units.append(SimCall(name="new-worker"))
        refetched = Inflight(job, instruction, (3,))
        refetched.values = {"x": 1}
        refetched.invocation_id = invocation_id(
            job.job_execution_id,
            entry.region.semantic_hash,
            job.stats["execution"]["hash"],
            instruction.addr - entry.region.start,
            (3,),
        )
        job.unfinished = 1
        reused = machine.dispatcher.take_reused(refetched, digest(refetched.values))
        assert reused is (change == "none")
        assert refetched.attempt_ids == []
        assert refetched.resolution == ("squash_reuse" if reused else "not_submitted")
        if change != "none":
            machine.dispatcher.keep_for_reuse(completed, 10, None)
            assert job.memo.n == 1  # crossed history cannot insert another stale result
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_caught_inline_faults_obey_record_limit_and_drop_both_sidecars(core):
    library = NodeLibrary("limited_failures")

    @library.node(outputs=["result"])
    def fail(x: int) -> int:
        raise ValueError("caught")

    source = """@def ()main(result):
    result = 0
    ([1, 2, 3])each(item, `body`)
    body:
        @try:
            (item)fail(result)
        @except error:
            result = 0
"""
    machine = Machine(
        program=source,
        entry="main",
        libraries=[library],
        bind={"*": Inline()},
        record=1.0,
        record_limit=1,
        core=core,
    )
    try:
        record = machine.submit({}).result(5)
        assert record.ok and record.record is None
        assert "record_origins" not in record.stats and "record_invocations" not in record.stats
        assert record.stats["record_skipped"] == "more than 1 calls"
    finally:
        machine.stop()


def test_repeated_loop_occurrences_have_distinct_ids_despite_equal_inputs():
    source = """@def ()main(values):
    ([1, 1, 1])collect(item, `body`, value -> values)
    body:
        (item)identity(value)
"""
    machine = Machine(program=source, entry="main", bind={"*": Inline()}, record=1.0, core="python")
    try:
        record = machine.submit({}, key="loop").result(5)
        assert record.ok and record.outputs == {"values": [1, 1, 1]}
        ids = record.stats["record_invocations"]
        assert len(ids) == len(record.record) == 3
        assert len({item["invocation_id"] for item in ids}) == 3
        assert len({item["attempt_ids"][0] for item in ids}) == 3
        assert len({call[1] for call in record.record}) == 1
    finally:
        machine.stop()


def test_preissue_operand_fault_keeps_one_logical_key_and_no_execution_attempt():
    machine = Machine(
        program="@def ()main(y):\n    (1)identity(y)\n",
        entry="main",
        bind={"*": Inline()},
        record=1.0,
        core="python",
    )
    try:
        region = machine.code.entries["main"].region
        instruction = next(item for item in machine.code.calls(region) if item.op == Op.CALL)
        instruction.args = [("value", ("missing", "value"))]
        record = machine.submit({}, key="bad").result(5)
        assert not record.ok and record.failure["kind"] == "KirRuntimeError"
        [sidecar] = record.stats["record_invocations"]
        assert sidecar["attempt_ids"] == [] and sidecar["resolution"] == "not_submitted"
        assert sidecar["invocation_id"] == invocation_id(
            record.stats["job_execution_id"],
            region.semantic_hash,
            record.stats["execution"]["hash"],
            instruction.addr - region.start,
            (0,),
        )
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_inline_failure_records_attempt_and_record_limit_drops_both_sidecars(core):
    library = NodeLibrary("inline_failure")

    @library.node(outputs=["y"])
    def fail(x: int) -> int:
        raise ValueError("expected")

    machine = Machine(
        program="@def (x)main(y):\n    (x)fail(y)\n",
        entry="main",
        libraries=[library],
        bind={"*": Inline()},
        record=1.0,
        core=core,
    )
    try:
        record = machine.submit({"x": 1}).result(5)
        assert record.failure["kind"] == "ValueError"
        [sidecar] = record.stats["record_invocations"]
        assert sidecar["resolution"] == "executed" and len(sidecar["attempt_ids"]) == 1
    finally:
        machine.stop()
    machine = Machine(
        program="@def (x)main(y):\n    (x)identity(a)\n    (a)identity(y)\n",
        entry="main",
        bind={"*": Inline()},
        record=1.0,
        record_limit=1,
        core=core,
    )
    try:
        record = machine.submit({"x": 1}).result(5)
        assert record.ok and record.record is None
        assert "record_invocations" not in record.stats and "record_origins" not in record.stats
    finally:
        machine.stop()
