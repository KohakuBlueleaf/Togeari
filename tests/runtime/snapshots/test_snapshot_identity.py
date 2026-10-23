"""Snapshot v3 alone preserves logical-run identity; fresh equal-input jobs never recover it."""

import pickle

import pytest
from kohakunode import NodeLibrary

from togeari import Inline, Machine, Use
from togeari.core.contracts.digest import digest
from togeari.core.contracts.io import MISS
from togeari.core.runtime.retire import snapshot
from togeari.core.runtime.retire.codec import encode_snapshot, prepare_restore
from togeari.core.runtime.state import Job, NameMap, Strand
from togeari.stores import MemoryStore
from togeari.testing import SimCall

SOURCE = "@def (x: int)main(y):\n    (x)sample(y)\n"


def state_for(machine, uid):
    entry = machine.code.entries["main"]
    job = Job("same", entry, {"x": 1})
    return {
        "job_execution_id": uid,
        "pc": entry.addr,
        "prefix": (),
        "counter": 0,
        "frames": [],
        "values": {"x": 1},
        "stats": dict(job.stats),
        "steps": 0,
        "record": [],
    }


def build(core="python", store=None, relocated=False):
    calls = []
    library = NodeLibrary("snapshot_identity")

    @library.node(outputs=["y"])
    def sample(x: int) -> int:
        calls.append(x)
        return len(calls)

    programs = {}
    if relocated:
        programs["@def ()padding(value):\n    value = 1\n"] = ["padding"]
    programs[SOURCE] = ["main"]
    machine = Machine(
        programs=programs,
        entry="main",
        libraries=[library],
        bind={
            "sample": Use(
                SimCall(name="sample-worker", latency_ms=0.2),
                store="positional",
                binding_revision="sampling.bundle/1",
                deterministic=False,
            )
        },
        store=store if store is not None else MemoryStore(),
        snapshot_every=1,
        record=1.0,
        persistence_compatibility="strict",
        core=core,
    )
    return machine, calls


@pytest.mark.parametrize("producer", ["python", "rust"])
@pytest.mark.parametrize("consumer", ["python", "rust"])
def test_only_compatible_snapshot_restores_same_invocation_and_positional_result(
    producer, consumer
):
    store = MemoryStore()
    machine, calls = build(producer, store)
    try:
        first = machine.submit({"x": 1}, key="same").result(10)
        second = machine.submit({"x": 1}, key="same").result(10)
        assert first.ok and second.ok
        assert first.outputs == {"y": 1} and second.outputs == {"y": 2}
        assert calls == [1, 1]
        assert first.stats["job_execution_id"] != second.stats["job_execution_id"]
        assert (
            first.stats["record_invocations"][0]["invocation_id"]
            != second.stats["record_invocations"][0]["invocation_id"]
        )
        entry = machine.code.entries["main"]
        uid = first.stats["job_execution_id"]
        raw = state_for(machine, uid)
        raw["stats"]["job_execution_id"] = "0" * 32
        blob = encode_snapshot(raw, entry, machine.code, entry.region.execution)
        assert pickle.loads(blob)["version"] == 3
        store.put(snapshot.snapshot_key(entry.region.semantic_hash, "same", digest({"x": 1})), blob)
    finally:
        machine.stop()
    resumed_machine, resumed_calls = build(consumer, store, relocated=True)
    try:
        resumed = resumed_machine.submit({"x": 1}, key="same").result(10)
        assert resumed.ok and resumed.outputs == first.outputs
        assert resumed.stats["resumed"] is True
        assert resumed.stats["job_execution_id"] == uid
        assert resumed_calls == []
        [sidecar] = resumed.stats["record_invocations"]
        assert sidecar["invocation_id"] == first.stats["record_invocations"][0]["invocation_id"]
        assert sidecar["resolution"] == "positional_hit" and sidecar["attempt_ids"] == []
    finally:
        resumed_machine.stop()


@pytest.mark.parametrize("bad_id", [None, True, 1, "", "a" * 31, "A" * 32, "z" * 32, "a" * 64])
def test_malformed_authoritative_uid_rejects_without_job_mutation(bad_id):
    machine, _ = build()
    try:
        entry = machine.code.entries["main"]
        job = Job("same", entry, {})
        original_id = job.job_execution_id
        job.add_strand(Strand(job, entry.addr, (), 0, (), NameMap()))
        original_stats = dict(job.stats)
        strands = dict(job.strands)
        envelope = pickle.loads(
            encode_snapshot(
                state_for(machine, original_id), entry, machine.code, entry.region.execution
            )
        )
        envelope["state"]["job_execution_id"] = bad_id
        assert not snapshot.restore(job, pickle.dumps(envelope), machine.code, mode="strict")
        assert job.job_execution_id == original_id and job.stats == original_stats
        assert job.strands == strands
    finally:
        machine.stop()


@pytest.mark.parametrize("version", [1, 2])
def test_older_snapshots_are_diagnosed_never_given_a_new_logical_identity(version):
    machine, _ = build()
    try:
        entry = machine.code.entries["main"]
        envelope = pickle.loads(
            encode_snapshot(
                state_for(machine, "1" * 32), entry, machine.code, entry.region.execution
            )
        )
        envelope["version"] = version
        del envelope["state"]["job_execution_id"]
        decision = prepare_restore(
            pickle.dumps(envelope), entry, machine.code, entry.region.execution, "warn"
        )["decision"]
        assert decision["allow"] is False and decision["issues"][0]["code"] == "migration_needed"
    finally:
        machine.stop()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("core", ["python", "rust"])
def test_old_locator_hits_reject_before_sampling(version, core):
    store = MemoryStore()
    machine, calls = build(core, store)
    try:
        entry = machine.code.entries["main"]
        old_key = snapshot.legacy_snapshot_key if version == 1 else snapshot.v2_snapshot_key
        store.put(old_key(entry.region.semantic_hash, "same", digest({"x": 1})), b"legacy")
        record = machine.submit({"x": 1}, key="same").result(10)
        assert not record.ok and record.failure["kind"] == "SnapshotIncompatible"
        assert record.stats["snapshot_restore"]["code"] == "migration_needed"
        assert calls == []
    finally:
        machine.stop()


@pytest.mark.parametrize(
    "bad_sidecar",
    [
        [],
        (),
        [None, None],
        [{"invocation_id": "bad", "attempt_ids": [], "resolution": "not_submitted"}],
        [{"invocation_id": "a" * 64, "attempt_ids": ["bad"], "resolution": "executed"}],
        [
            {
                "invocation_id": "a" * 64,
                "attempt_ids": ["b" * 32, "b" * 32],
                "resolution": "executed",
            }
        ],
        [{"invocation_id": "a" * 64, "attempt_ids": [], "resolution": "executed"}],
        [{"invocation_id": "a" * 64, "attempt_ids": ["b" * 32], "resolution": "positional_hit"}],
        [{"invocation_id": "a" * 64, "attempt_ids": [], "resolution": "unknown"}],
        [{"invocation_id": "a" * 64, "attempt_ids": [], "resolution": "not_submitted", "extra": 1}],
    ],
    ids=lambda _: "bad-sidecar",
)
def test_corrupt_invocation_sidecars_reject_before_restore(bad_sidecar):
    machine, _ = build()
    try:
        entry = machine.code.entries["main"]
        raw = state_for(machine, "1" * 32)
        raw["record"] = [("sample", "digest", "ok", 1, None)]
        envelope = pickle.loads(encode_snapshot(raw, entry, machine.code, entry.region.execution))
        envelope["state"]["stats"]["record_invocations"] = bad_sidecar
        result = prepare_restore(
            pickle.dumps(envelope), entry, machine.code, entry.region.execution, "strict"
        )
        assert not result["decision"]["allow"] and result["state"] is None
    finally:
        machine.stop()


@pytest.mark.parametrize("destination_recording", [False, True])
def test_dropping_saved_trace_clears_sidecars_and_still_allows_next_capture(destination_recording):
    machine, _ = build()
    try:
        entry = machine.code.entries["main"]
        raw = state_for(machine, "1" * 32)
        raw["record"] = [("sample", "digest", "ok", 1, None)] if not destination_recording else None
        if raw["record"] is not None:
            raw["stats"]["record_origins"] = [None]
            raw["stats"]["record_invocations"] = [
                {
                    "invocation_id": "a" * 64,
                    "attempt_ids": ["b" * 32],
                    "resolution": "executed",
                }
            ]
        job = Job("same", entry, {})
        job.record = [] if destination_recording else None
        blob = encode_snapshot(raw, entry, machine.code, entry.region.execution)
        assert snapshot.restore(job, blob, machine.code, mode="strict")
        assert job.job_execution_id == "1" * 32 and job.record is None
        assert "record_invocations" not in job.stats and "record_origins" not in job.stats
        assert snapshot.capture(job, machine.code) is not None
    finally:
        machine.stop()


@pytest.mark.parametrize("direct_mutator", [False, True])
@pytest.mark.parametrize("core", ["python", "rust"])
def test_same_invocation_recovery_preserves_mutation_safeguards(core, direct_mutator):
    calls = []
    library = NodeLibrary("alias_recovery")

    @library.node(outputs=["xs"])
    def mklist(x: int) -> list:
        calls.append("mklist")
        return [x]

    @library.node(mutates=["xs"], outputs=["n"])
    def push(xs: list) -> int:
        calls.append("push")
        xs.append(99)
        return len(xs)

    if direct_mutator:
        source = "@def (xs: list)main(n, result):\n    (xs)push(n)\n    result = xs\n"
        inputs = {"xs": [7]}
        bindings = {
            "push": Use(SimCall(name="push"), store="positional", binding_revision="push/1")
        }
    else:
        source = (
            "@def (x: int)main(n, result):\n    (x)mklist(xs)\n    (xs)push(n)\n    result = xs\n"
        )
        inputs = {"x": 7}
        bindings = {
            "mklist": Use(SimCall(name="mklist"), store="positional", binding_revision="mklist/1"),
            "push": Inline(binding_revision="push/1"),
        }
    store = MemoryStore()
    machine = Machine(
        program=source,
        entry="main",
        libraries=[library],
        bind=bindings,
        store=store,
        snapshot_every=1,
        record=1.0,
        core=core,
        persistence_compatibility="strict",
    )
    try:
        entry = machine.code.entries["main"]
        input_digest = digest(inputs)
        pristine = {"xs": [7]} if direct_mutator else {"x": 7}
        first = machine.submit(inputs, key="same").result(10)
        assert first.ok and first.outputs == {"n": 2, "result": [7, 99]}
        if direct_mutator:
            assert any(
                "mutating" in warning.message and "ignored" in warning.message
                for warning in machine.warnings
            )
            assert len(store) == 0
        else:
            assert list(store.data.values()) == [[7]]
        uid = first.stats["job_execution_id"]
        raw = state_for(machine, uid)
        raw["values"] = pristine
        store.put(
            snapshot.snapshot_key(entry.region.semantic_hash, "same", input_digest),
            encode_snapshot(raw, entry, machine.code, entry.region.execution),
        )
        calls.clear()
        second = machine.submit({"xs": [7]} if direct_mutator else {"x": 7}, key="same").result(10)
        assert second.ok and second.outputs == first.outputs
        assert second.stats["resumed"] and second.stats["job_execution_id"] == uid
        assert calls == ["push"]
        first_call = second.stats["record_invocations"][0]
        assert first_call["resolution"] == ("executed" if direct_mutator else "positional_hit")
        assert bool(first_call["attempt_ids"]) is direct_mutator
    finally:
        machine.stop()


def test_v3_candidate_has_precedence_over_both_legacy_locators():
    store = MemoryStore()
    store.put(b"v3", b"current")
    store.put(b"v2", b"older")
    store.put(b"v1", b"oldest")
    assert snapshot.load_candidate(store, b"v3", b"v1", v2_key=b"v2")["blob"] == b"current"
    store.delete(b"v3")
    assert snapshot.load_candidate(store, b"v3", b"v1", v2_key=b"v2")["action"] == "reject"
    assert store.get(b"v3") is MISS
