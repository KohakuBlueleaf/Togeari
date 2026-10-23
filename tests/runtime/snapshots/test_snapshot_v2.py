"""Shared snapshot2 preflight: relative addressing, atomic rejection and alias preservation."""

import copy
import pickle

import pytest
from kohakunode import NodeLibrary

from togeari import BasePlugin, Inline, Machine, Use
from togeari.conformance import conform
from togeari.core.contracts.digest import digest
from togeari.core.contracts.io import MISS
from togeari.core.program.decode.instr import Op
from togeari.core.runtime.retire import snapshot
from togeari.core.runtime.retire.codec import encode_snapshot, prepare_restore
from togeari.core.runtime.state import Job, NameMap, Strand, const
from togeari.stores import MemoryStore
from togeari.testing import SimCall

PROGRAM = """@def (items: list)main(result):
    result = 0
    (items)each(item, `body`)
    body:
        (item, 0)greater(positive)
        (positive)branch(`yes`, `no`)
        yes:
            (result, item)add(result)
        no:
"""


@pytest.fixture
def machine():
    machine = Machine(program=PROGRAM, entry="main", bind={"*": Inline()}, core="python")
    yield machine
    machine.stop()


def raw_state(machine, frame_kind=None):
    entry = machine.code.entries["main"]
    region = entry.region
    job = Job("job", entry, {})
    raw = {
        "job_execution_id": job.job_execution_id,
        "pc": entry.addr,
        "prefix": (3, 2),
        "counter": 7,
        "frames": [],
        "values": {"items": [1, 2], "result": 0},
        "stats": dict(job.stats),
        "steps": 9,
        "record": [],
    }
    if frame_kind:
        op = Op.EACH if frame_kind == "iter" else Op.BRANCH
        ins = next(ins for ins in machine.code.mem[region.start : region.end] if ins.op == op)
        frame = {
            "reconv": region.tables.reconv.get(ins.addr),
            "saved": ((2, 1), 4),
            "branch": ins.addr,
            "kind": frame_kind,
            "each": None,
            "items": None,
            "i": 0,
            "base": None,
            "joined": False,
        }
        if frame_kind == "iter":
            frame.update(each=ins.addr, items=[1, 2], base=(2, 0), i=1)
            raw["pc"] = ins.body
        else:
            raw["pc"] = ins.targets[0]
        raw["frames"] = [frame]
    return raw


def blob_for(machine, state=None):
    entry = machine.code.entries["main"]
    state = raw_state(machine) if state is None else state
    return encode_snapshot(state, entry, machine.code, entry.region.execution)


def prepare(machine, blob, mode="strict"):
    entry = machine.code.entries["main"]
    return prepare_restore(blob, entry, machine.code, entry.region.execution, mode)


@pytest.mark.parametrize("frame_kind", [None, "branch", "iter"])
def test_relative_addresses_preserve_all_logical_keys_when_region_moves(machine, frame_kind):
    raw = raw_state(machine, frame_kind)
    blob = blob_for(machine, raw)
    envelope = pickle.loads(blob)
    assert (
        envelope["version"] == snapshot.VERSION and envelope["addressing"] == "region-relative-v1"
    )
    assert "version" not in envelope["state"]
    assert envelope["state"]["prefix"] == raw["prefix"]
    prefix = "@def ()padding(value):\n    a = 1\n    b = 2\n    (a, b)add(value)\n"
    moved = Machine(
        programs={prefix: ["padding"], PROGRAM: ["main"]}, bind={"*": Inline()}, core="python"
    )
    try:
        old_region = machine.code.entries["main"].region
        new_region = moved.code.entries["main"].region
        assert old_region.start != new_region.start
        assert old_region.execution["hash"] == new_region.execution["hash"]
        result = prepare(moved, blob)
        assert result["decision"]["status"] == "compatible"
        state = result["state"]
        delta = new_region.start - old_region.start
        assert state["pc"] == raw["pc"] + delta
        assert (state["prefix"], state["counter"]) == (raw["prefix"], raw["counter"])
        for before, after in zip(raw["frames"], state["frames"]):
            for name in ("saved", "base", "i", "items"):
                assert after[name] == before[name]
            for name in ("branch", "each", "reconv"):
                expected = before[name] if before[name] in (None, -1) else before[name] + delta
                assert after[name] == expected
    finally:
        moved.stop()


def test_joined_iteration_reconvergence_is_recomputed_after_relocation(machine):
    raw = raw_state(machine, "iter")
    frame = raw["frames"][0]
    each = machine.code.mem[frame["each"]]
    frame["joined"] = True
    frame["reconv"] = each.iter_end
    result = prepare(machine, blob_for(machine, raw))
    assert result["decision"]["allow"]
    assert result["state"]["frames"][0]["reconv"] == machine.code.tables(each.addr).reconv.get(
        each.addr
    )


class Opaque:
    def __deepcopy__(self, memo):
        raise AssertionError("snapshot preparation must preserve unpickled values without copying")


def test_alias_graph_huge_user_integers_and_opaque_plugin_stats_survive(machine):
    raw = raw_state(machine, "iter")
    shared = [10**5000, Opaque()]
    shared.append(shared)
    raw["values"] = {"left": shared, "right": shared}
    raw["frames"][0]["items"] = shared
    raw["stats"]["plugin_value"] = shared
    raw["record"] = [
        (
            "node",
            None,
            "error",
            {"kind": "ValueError", "node": "node", "message": "preissue"},
            None,
        ),
        ("node", "digest", "ok", shared, {"port": shared}),
    ]
    state = prepare(machine, blob_for(machine, raw))["state"]
    assert state is not None
    values = state["values"]
    assert values["left"] is values["right"] is state["frames"][0]["items"]
    assert values["left"] is state["stats"]["plugin_value"] is state["record"][1][3]
    assert state["record"][1][4]["port"] is values["left"]
    assert values["left"][2] is values["left"]
    assert values["left"][0] == 10**5000


MALFORMED = [
    (("pc",), True),
    (("pc",), -1),
    (("pc",), 2**63),
    (("pc",), 999999),
    (("counter",), False),
    (("counter",), -1),
    (("counter",), 2**63),
    (("prefix",), [1]),
    (("prefix",), (True,)),
    (("prefix",), (2**63,)),
    (("steps",), -1),
    (("steps",), 1.5),
    (("values",), {1: "bad"}),
    (("stats", "retired"), True),
    (("stats", "retired"), 1.5),
    (("stats", "retired"), 2**63),
    (("stats", "program"), 1),
    (("stats", "resumed"), 1),
    (("stats", "execution_epoch"), -1),
    (("frames", 0, "kind"), "unknown"),
    (("frames", 0, "joined"), 1),
    (("frames", 0, "saved"), ((), True)),
    (("frames", 0, "saved"), [(), 1]),
    (("frames", 0, "base"), (False,)),
    (("frames", 0, "i"), -1),
    (("frames", 0, "i"), 2),
    (("frames", 0, "each"), None),
    (("frames", 0, "reconv"), 999999),
    (("frames", 0, "items"), (1, 2)),
    (("record",), [()]),
    (("record",), [("n", 42, "ok", None, None)]),
    (("record",), [("n", "d", "bad", None, None)]),
]


def change(state, path, value):
    target = state
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value


@pytest.mark.parametrize("path,value", MALFORMED, ids=lambda _: "malformed")
def test_invalid_structures_reject_without_mutating_job_or_code(machine, path, value):
    envelope = pickle.loads(blob_for(machine, raw_state(machine, "iter")))
    change(envelope["state"], path, value)
    blob = pickle.dumps(envelope)
    entry = machine.code.entries["main"]
    job = Job("job", entry, {})
    original = Strand(job, entry.addr, (), 0, (), NameMap({"kept": const(7)}))
    job.add_strand(original)
    before = (dict(job.strands), dict(job.stats), job.steps, job.record, len(machine.code.mem))
    result = prepare(machine, blob)
    assert not result["decision"]["allow"] and result["state"] is None
    assert not snapshot.restore(job, blob, machine.code, mode="strict")
    assert before == (
        dict(job.strands),
        dict(job.stats),
        job.steps,
        job.record,
        len(machine.code.mem),
    )
    assert original.names.get("kept").value == 7


def test_missing_fields_bad_frame_relationships_and_versions_reject(machine):
    for path in [("stats", "calls"), ("pc",), ("frames", 0, "joined")]:
        envelope = pickle.loads(blob_for(machine, raw_state(machine, "iter")))
        target = envelope["state"]
        for part in path[:-1]:
            target = target[part]
        del target[path[-1]]
        assert not prepare(machine, pickle.dumps(envelope))["decision"]["allow"]
    envelope = pickle.loads(blob_for(machine, raw_state(machine, "iter")))
    frame = envelope["state"]["frames"][0]
    frame["each"] = frame["branch"] = 0
    assert not prepare(machine, pickle.dumps(envelope))["decision"]["allow"]
    for blob in (
        b"broken",
        b"",
        pickle.dumps({"version": 1}),
        pickle.dumps({"version": True}),
        pickle.dumps({"version": 99}),
    ):
        assert not prepare(machine, blob)["decision"]["allow"]


@pytest.mark.parametrize("pinned", [False, True])
def test_low_level_restore_preserves_local_identity_not_saved_epoch(machine, pinned):
    entry = machine.code.entries["main"]
    raw = raw_state(machine)
    raw["stats"].update(
        execution={"saved": True},
        execution_epoch=99,
        program_region=999,
        plugin_custom={"values": [1, 2]},
        _persistence_context=("foreign-machine", 99),
    )
    job = Job("job", entry, {})
    if pinned:
        job.stats.update(
            execution=entry.region.execution, execution_epoch=0, program_region=entry.region.index
        )
    job.add_strand(Strand(job, entry.addr, (), 0, (), NameMap()))
    assert snapshot.restore(job, blob_for(machine, raw), machine.code, mode="strict")
    assert job.stats["plugin_custom"] == {"values": [1, 2]}
    assert "_persistence_context" not in job.stats
    if pinned:
        assert job.stats["execution"] is entry.region.execution
        assert job.stats["execution_epoch"] == 0
        assert job.stats["program_region"] == entry.region.index
    else:
        assert all(
            name not in job.stats for name in ("execution", "execution_epoch", "program_region")
        )


def test_capture_requires_actual_code_and_keeps_collector_gate(machine):
    entry = machine.code.entries["main"]
    job = Job("job", entry, {})
    job.add_strand(Strand(job, entry.addr, (), 0, (), NameMap()))
    with pytest.raises(ValueError, match="CodeMemory"):
        snapshot.capture(job)
    job.collectors[()] = object()
    assert snapshot.capture(job) is None
    job.collectors.clear()
    assert snapshot.capture(job, machine.code) is not None


class ReadStore:
    def __init__(self, values=None, fail=False):
        self.values = values or {}
        self.keys = []
        self.fail = fail

    def get(self, key):
        self.keys.append(key)
        if self.fail:
            raise OSError("unreadable")
        return self.values.get(key, MISS)


def test_candidates_distinguish_absence_v2_v1_and_store_failure():
    new, old = b"v2", b"v1"
    absent = ReadStore()
    assert snapshot.load_candidate(absent, new, old)["action"] == "new"
    assert absent.keys == [new, old]
    hit = ReadStore({new: b"bad-v2", old: b"legacy"})
    assert snapshot.load_candidate(hit, new, old)["blob"] == b"bad-v2"
    assert hit.keys == [new]
    legacy = snapshot.load_candidate(ReadStore({old: b"old"}), new, old)
    assert legacy["action"] == "reject"
    assert legacy["decision"]["issues"][0]["code"] == "migration_needed"
    seen = []
    failing = ReadStore(fail=True)
    denied = snapshot.load_candidate(failing, new, old, on_error=seen.append)
    assert denied["action"] == "reject" and len(seen) == 1
    assert failing.keys == [new]
    assert snapshot.snapshot_key("p", "k", b"i") != snapshot.legacy_snapshot_key("p", "k", b"i")


def test_snapshot_put_failure_is_not_acknowledged(machine):
    class BadPut(MemoryStore):
        def put(self, key, value):
            raise OSError("cannot save")

    dispatcher = machine.dispatcher
    dispatcher.store = BadPut()
    entry = machine.code.entries["main"]
    job = Job("job", entry, {})
    job.inputs_dig = digest({})
    job.stats["retired"] = 1
    job.add_strand(Strand(job, entry.addr, (), 0, (), NameMap()))
    dispatcher.take_snapshot(job)
    assert job.snap_at == 0 and not job.stats.get("snapshots")
    assert dispatcher.stats["store_errors"] == 1
    assert not job.snap_off


def test_encode_refuses_invalid_and_mixed_execution_history(machine):
    entry = machine.code.entries["main"]
    manifest = copy.deepcopy(entry.region.execution)
    manifest["completeness"]["complete"] = False
    manifest["completeness"]["transitions"] = [{"kind": "changed", "positions": []}]
    with pytest.raises(ValueError, match="not persistable"):
        encode_snapshot(raw_state(machine), entry, machine.code, manifest)
    manifest = copy.deepcopy(entry.region.execution)
    manifest["hash"] = "tampered"
    with pytest.raises(ValueError, match="not persistable"):
        encode_snapshot(raw_state(machine), entry, machine.code, manifest)


LOOP = """@def (n: int)main(x):
    x = 0
    ()jump(`top`)
    top:
        (x)step(x)
        (x, n)less(more)
        (more)branch(`again`, `done`)
        again:
            ()jump(`top`)
        done:
"""
PAD = "@def ()padding(value):\n    a = 1\n    b = 2\n    (a, b)add(value)\n"


@pytest.mark.parametrize("producer", ["python", "rust"])
@pytest.mark.parametrize("consumer", ["python", "rust"])
def test_all_core_snapshot_pairs_resume_after_region_relocation(producer, consumer):
    library = NodeLibrary("snapshot_pairs")
    calls = []
    fail = [True]

    @library.node(ordered=True)
    def step(x: int) -> int:
        calls.append(x)
        if fail[0] and x == 4:
            raise ValueError("pause at four")
        return x + 1

    store = MemoryStore()

    def build(core, relocated):
        programs = {PAD: ["padding"], LOOP: ["main"]} if relocated else {LOOP: ["main"]}
        return Machine(
            programs=programs,
            entry="main",
            libraries=[library],
            bind={
                "step": Use(
                    SimCall(name="work", latency_ms=0.2, tail=0),
                    binding_revision="test.bundle/1",
                    implementation_revision="step/1",
                ),
                "kir.std.*": Inline(),
            },
            store=store,
            snapshot_every=1,
            record=1.0,
            run_ahead=False,
            persistence_compatibility="strict",
            core=core,
        )

    first_machine = build(producer, False)
    try:
        first = first_machine.submit({"n": 8}, key="job").result(15)
        old_region = first_machine.code.entries["main"].region
        first_start = old_region.start
        key = snapshot.snapshot_key(old_region.semantic_hash, "job", digest({"n": 8}))
    finally:
        first_machine.stop()
    assert not first.ok and first.failure["kind"] == "ValueError"
    assert first.stats.get("snapshots", 0) > 0
    blob = store.get(key)
    assert type(blob) is bytes
    envelope = pickle.loads(blob)
    assert envelope["version"] == snapshot.VERSION
    saved_uid = envelope["state"]["job_execution_id"]
    assert saved_uid == first.stats["job_execution_id"]
    saved_record = envelope["state"]["record"]
    saved_invocations = envelope["state"]["stats"]["record_invocations"]
    assert saved_record and len(saved_record) == len(saved_invocations)
    prior_attempts = {
        attempt for item in first.stats["record_invocations"] for attempt in item["attempt_ids"]
    }
    restart_x = envelope["state"]["values"]["x"]
    assert 0 < restart_x <= 4
    calls.clear()
    fail[0] = False
    second_machine = build(consumer, True)
    try:
        assert second_machine.code.entries["main"].region.start != first_start
        second = second_machine.submit({"n": 8}, key="job").result(15)
        assert second.ok, second.failure
        assert second.outputs == {"x": 8}
        assert second.stats["resumed"] is True
        assert second.stats["snapshot_restore"]["status"] == "compatible"
        assert second.stats["job_execution_id"] == saved_uid
        prefix_count = len(saved_record)
        assert second.record[:prefix_count] == saved_record
        assert second.stats["record_invocations"][:prefix_count] == saved_invocations
        assert len(second.record) == len(second.stats["record_invocations"])
        suffix = second.stats["record_invocations"][prefix_count:]
        assert suffix and all(item["resolution"] == "executed" for item in suffix)
        later_attempts = {attempt for item in suffix for attempt in item["attempt_ids"]}
        assert later_attempts and not later_attempts & prior_attempts
        assert calls == list(range(restart_x, 8))
        assert conform(second_machine.code.entries["main"].region.job, second, {"n": 8}).ok
        assert store.get(key) is MISS
    finally:
        second_machine.stop()


@pytest.mark.parametrize("mode", ["warn", "strict"])
@pytest.mark.parametrize("core", ["python", "rust"])
def test_incomplete_snapshot_identity_warns_or_rejects_without_restarting(mode, core):
    calls = []
    library = NodeLibrary("unknown_snapshot")

    @library.node()
    def work(x: int) -> int:
        calls.append(x)
        return x + 1

    source = "@def (x: int)main(y):\n    (x)work(y)\n"
    store = MemoryStore()
    machine = Machine(
        program=source,
        entry="main",
        libraries=[library],
        bind={"*": Inline()},
        store=store,
        snapshot_every=1,
        persistence_compatibility=mode,
        core=core,
    )
    try:
        entry = machine.code.entries["main"]
        manifest = entry.region.execution
        assert not manifest["completeness"]["complete"]
        raw = raw_state(machine)
        raw["values"] = {"x": 3}
        raw["prefix"], raw["counter"] = (), 0
        raw["stats"]["execution_epoch"] = 987654
        blob = blob_for(machine, raw)
        key = snapshot.snapshot_key(entry.region.semantic_hash, "job", digest({"x": 3}))
        store.put(key, blob)
        record = machine.submit({"x": 3}, key="job").result(10)
        if mode == "warn":
            assert record.ok and record.outputs == {"y": 4}
            assert calls == [3]
            assert record.stats["resumed"] is True
            assert record.stats["snapshot_restore"]["status"] == "unverified"
            assert record.stats["execution_epoch"] != 987654
            assert machine._persistence_warnings
        else:
            assert not record.ok and record.failure["kind"] == "SnapshotIncompatible"
            assert record.stats["snapshot_restore"]["code"] == "identity_incomplete"
            assert calls == []
    finally:
        machine.stop()


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("core", ["python", "rust"])
def test_output_manifest_is_detached_from_cache_and_future_jobs(core, resumed):
    store = MemoryStore()
    machine = Machine(
        program=PROGRAM,
        entry="main",
        bind={"*": Inline()},
        core=core,
        store=store,
        snapshot_every=1,
    )
    try:
        entry = machine.code.entries["main"]
        expected = copy.deepcopy(entry.region.execution)
        inputs = {"items": [1, 2]}
        if resumed:
            raw = raw_state(machine)
            raw["prefix"], raw["counter"] = (), 0
            key = snapshot.snapshot_key(entry.region.semantic_hash, "first", digest(inputs))
            store.put(key, blob_for(machine, raw))
        first = machine.submit(inputs, key="first").result(10)
        assert first.ok
        if resumed:
            assert first.stats["resumed"]
        first.stats["execution"]["identity"]["calls"].clear()
        first.stats["execution"]["completeness"]["complete"] = False
        first.stats["execution"]["hash"] = "edited output"
        second = machine.submit(inputs, key="second").result(10)
        assert second.ok and second.stats["execution"] == expected
        assert entry.region.execution == expected
    finally:
        machine.stop()


class AdmissionProbe(BasePlugin):
    def __init__(self):
        self.started = []

    def on_job_start(self, info):
        self.started.append(info)


@pytest.mark.parametrize(
    "candidate", ["v1", "corrupt", "read_error", "pc_bool", "bad_frame", "stats_bool"]
)
@pytest.mark.parametrize("core", ["python", "rust"])
def test_denied_candidates_fail_before_admission_or_host_effects(candidate, core):
    store = MemoryStore()
    probe = AdmissionProbe()
    machine = Machine(
        program=PROGRAM,
        entry="main",
        bind={"*": Inline()},
        store=store,
        snapshot_every=1,
        plugins=[probe],
        core=core,
    )
    try:
        entry = machine.code.entries["main"]
        inputs = {"items": [1, 2]}
        program_hash = entry.region.semantic_hash
        key = snapshot.snapshot_key(program_hash, "k", digest(inputs))
        if candidate == "v1":
            key = snapshot.legacy_snapshot_key(program_hash, "k", digest(inputs))
            store.put(key, pickle.dumps({"version": 1}))
        elif candidate == "corrupt":
            store.put(key, b"broken")
        elif candidate == "read_error":
            store.get = ReadStore(fail=True).get
        else:
            envelope = pickle.loads(blob_for(machine, raw_state(machine, "iter")))
            match candidate:
                case "pc_bool":
                    envelope["state"]["pc"] = True
                case "bad_frame":
                    envelope["state"]["frames"][0]["kind"] = "bogus"
                case "stats_bool":
                    envelope["state"]["stats"]["retired"] = False
            store.put(key, pickle.dumps(envelope))
        record = machine.submit(inputs, key="k").result(10)
        assert not record.ok and record.failure["kind"] == "SnapshotIncompatible"
        assert set(record.failure) == {"kind", "node", "message"}
        assert record.stats["snapshot_restore"]["status"] == "rejected"
        assert probe.started == []
        assert not machine.dispatcher.jobs
        assert machine.dispatcher.stats["machine_errors"] == 0
    finally:
        machine.stop()
