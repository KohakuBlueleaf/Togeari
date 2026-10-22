"""Records use ordinary calls, adapters, stores and snapshots on both Togeari cores."""

import copy
import json
import pickle

import httpx
import pytest
import record_nodes
from kohakunode import NodeLibrary
from kohakunode.records import RecordValue, construct, decode_record, encode_record, project
from kohakunode.sig.files import library_from_json, library_to_json, save_library

from togeari import Inline, Machine, SourceItem, Use
from togeari.adapters.http import JsonHttp
from togeari.cli.replay import job_program, replay_dir
from togeari.conformance import conform
from togeari.core.construct.errors import Problems
from togeari.core.contracts.digest import digest, digest_with_weak
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.results import ItemError, Ok
from togeari.core.program.job import build_job_programs
from togeari.engines.python import PythonCallEngine
from togeari.io import Iterable, Memory
from togeari.plugins import Archive, Recorder
from togeari.plugins.record.archive import program_info
from togeari.stores import MemoryStore

LIB = record_nodes.LIB
POINT = record_nodes.POINT
ENVELOPE = record_nodes.ENVELOPE
SCHEMAS = record_nodes.SCHEMAS

PROGRAM = """@use "recordtest" as r
@def (x: int, tags: list[int])main(original, changed, envelope, old_x, new_x):
    (x=x, tags=tags)r.Point.make(original)
    (original, 99)r.Point.with.x(changed)
    (changed)r.Envelope.make(envelope)
    (original)r.Point.get.x(old_x)
    (changed)r.Point.get.x(new_x)
"""


def binding(mode, *, store=None):
    if mode == "inline":
        return Inline()
    return Use(PythonCallEngine(workers=1, mode=mode, transport="pickle"), store=store)


def machine(program=PROGRAM, *, core, mode="inline", plugins=(), **knobs):
    return Machine(
        program=program,
        entry="main",
        libraries=[LIB],
        bind={"recordtest.*": binding(mode), "kir.std.*": Inline()},
        core=core,
        plugins=list(plugins),
        record=1.0,
        assert_alias=True,
        fingerprint_inputs=True,
        **knobs,
    )


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("mode", ["inline", "thread", "process"])
def test_generated_record_calls_execute_record_and_reexecute(tmp_path, core, mode):
    folder = tmp_path / "records"
    m = machine(core=core, mode=mode, plugins=[Recorder(folder, libraries=["record_nodes:LIB"])])
    inputs = {"x": 7, "tags": [1, 2]}
    try:
        record = m.submit(inputs, key="one").result(15)
        job = m.code.entries["main"].region.job
    finally:
        m.stop()
    assert record.ok, record.failure
    original, changed = record.outputs["original"], record.outputs["changed"]
    assert type(original) is type(changed) is RecordValue
    assert original.schema_id == "recordtest.Point"
    assert project(POINT, original, "x") == record.outputs["old_x"] == 7
    assert project(POINT, changed, "x") == record.outputs["new_x"] == 99
    assert project(POINT, original, "y") == 0
    assert project(ENVELOPE, record.outputs["envelope"], "point") == changed
    assert not record.stats.get("weak_digests")
    assert conform(job, record, inputs).ok
    assert conform(job, record, inputs, mode="reexecute", pure=lambda _: True).ok

    # Process transport and archive persistence preserve schema identity and strong digests.
    [archived] = Archive(folder).jobs()
    assert archived["weak"] == []
    assert digest(archived["outputs"]) == digest(record.outputs)
    replayed = list(replay_dir(folder, reexecute=[call[0] for call in record.record]))
    assert len(replayed) == 1 and replayed[0].verdict.ok, replayed


ALIASES = """@use "recordtest" as r
@def (tags: list[int])main(original, updated, after_original, after_updated, projected, tags):
    (x=1, tags=tags)r.Point.make(original)
    (tags, 8)r.push(tags)
    (original)r.Point.get.tags(projected)
    (projected, 9)r.push(projected)
    (original, projected)r.Point.with.tags(updated)
    (projected, 10)r.push(projected)
    (original)r.Point.get.tags(after_original)
    (updated)r.Point.get.tags(after_updated)
"""


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("mode", ["inline", "thread", "process"])
@pytest.mark.parametrize("mutates", ["copy-if-speculative", "copy-always", "wait"])
def test_mutable_aliases_cannot_change_existing_records(core, mode, mutates):
    inputs = {"tags": [1, 2]}
    m = machine(ALIASES, core=core, mode=mode, mutates=mutates)
    try:
        record = m.submit(copy.deepcopy(inputs), key="aliases").result(10)
        job = m.code.entries["main"].region.job
    finally:
        m.stop()
    assert record.ok, record.failure
    assert record.outputs["after_original"] == [1, 2]
    assert record.outputs["after_updated"] == [1, 2, 9]
    assert record.outputs["projected"] == [1, 2, 9, 10]
    assert record.outputs["tags"] == [1, 2, 8]
    assert sum(call[4] is not None for call in record.record) == 3
    if mutates == "copy-always":
        assert record.stats["mutates_copies"] == 3
    assert conform(job, record, copy.deepcopy(inputs)).ok
    # Re-execute the pure generated record operations; replay the effectful host mutation.
    assert conform(
        job,
        record,
        copy.deepcopy(inputs),
        mode="reexecute",
        pure=lambda name: name != "r.push",
    ).ok

    # Projecting again returns a fresh list, not the record's retained storage.
    value = record.outputs["original"]
    projected = project(POINT, value, "tags")
    projected.append(100)
    assert project(POINT, value, "tags") == [1, 2]


@pytest.mark.parametrize("modular", [False, True])
def test_record_contract_identity_and_archived_hash_are_mixed_once(tmp_path, monkeypatch, modular):
    source = PROGRAM
    if modular:
        module = tmp_path / "points.kir"
        module.write_text(
            '@use "recordtest" as r\n@export point\n'
            "@def (x: int)point(out):\n    (x)r.Point.make(out)\n",
            encoding="utf-8",
        )
        source = tmp_path / "main.kir"
        source.write_text(
            '@import "points.kir" as points\n@def (x: int)main(value):\n'
            "    (x)points.point(value)\n",
            encoding="utf-8",
        )
    problems = Problems()
    [job] = build_job_programs(source, ["main"], [LIB], None, None, problems)
    problems.raise_if_any()
    assert bool(job.dependencies["modules"]) == modular
    info = program_info(job, ["record_nodes:LIB"], None)
    if modular:
        source.unlink()
        module.unlink()
    rebuilt = job_program(json.loads(json.dumps(info)), [])
    assert rebuilt.semantic_hash == job.semantic_hash
    assert rebuilt.dependencies == job.dependencies

    # A schema default changes contract identity even if the executable AST is identical.
    changed_json = library_to_json(LIB)
    point = next(schema for schema in changed_json["records"] if schema["name"] == "Point")
    next(field for field in point["fields"] if field["name"] == "y")["default"] = 8
    changed = library_from_json(changed_json)
    monkeypatch.setattr(record_nodes, "LIB", changed)
    with pytest.raises(ValueError, match="signature contract differs: recordtest"):
        job_program(info, [])
    problems = Problems()
    [changed_job] = build_job_programs(PROGRAM, ["main"], [changed], None, None, problems)
    problems.raise_if_any()
    if not modular:
        assert changed_job.semantic_hash != job.semantic_hash
        assert changed_job.dependencies["hash"] != job.dependencies["hash"]


def test_strong_digest_pickle_and_store_copies_include_nominal_identity():
    values = [1, 2]
    point = construct(POINT, x=3, tags=values)
    envelope = construct(ENVELOPE, point=point, labels={"z": [1], "a": []})
    before, weak = digest_with_weak(envelope)
    assert weak == [] and not hasattr(envelope, "__dict__")
    values.append(99)
    assert digest(envelope) == before
    restored = pickle.loads(pickle.dumps(envelope, protocol=5))
    decoded = decode_record(json.loads(json.dumps(encode_record(envelope))), SCHEMAS)
    assert restored == decoded == envelope
    assert digest(restored) == digest(decoded) == before
    other = NodeLibrary("other")
    other_point = other.record("Point", [("x", int), ("y", int, 0), ("tags", "list[int]", [])])
    assert digest(construct(other_point, x=3, tags=[1, 2])) != digest(point)

    store = MemoryStore()
    store.put(b"record", envelope)
    first, second = store.get(b"record"), store.get(b"record")
    project(ENVELOPE, first, "labels")["z"].append(8)
    assert project(ENVELOPE, second, "labels") == {"a": [], "z": [1]}
    assert digest(first) == digest(second) == before


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("mode", ["inline", "thread"])
def test_signature_only_records_execute_without_host_implementations(tmp_path, core, mode):
    exported = library_to_json(LIB)
    assert exported["kirsig"] == 2 and exported["records"]
    loaded = library_from_json(json.loads(json.dumps(exported)))
    assert "echo" not in loaded.impls and "checkpoint" not in loaded.impls
    assert "Point.make" in loaded.impls and "Envelope.make" in loaded.impls
    save_library(loaded, tmp_path / "recordtest.kirsig.json")
    m = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[],
        sig_path=[tmp_path],
        bind={"recordtest.*": binding(mode)},
        core=core,
        record=1.0,
    )
    inputs = {"x": 5, "tags": [3]}
    try:
        record = m.submit(inputs, key="schema-only").result(10)
        job = m.code.entries["main"].region.job
    finally:
        m.stop()
    assert record.ok and record.outputs["old_x"] == 5 and record.outputs["new_x"] == 99
    assert conform(job, record, inputs, mode="reexecute", pure=lambda _: True).ok


@pytest.mark.parametrize("core", ["python", "rust"])
def test_record_inputs_use_content_store_without_weak_fallback(core):
    program = """@use "recordtest" as r
@def (record: recordtest.Point)main(value):
    (record)r.Point.get.x(value)
"""
    store = MemoryStore()
    point = construct(POINT, x=42)
    results = []
    for _ in range(2):
        m = Machine(
            program=program,
            entry="main",
            libraries=[LIB],
            bind={"recordtest.*": binding("thread", store="content")},
            core=core,
            record=1.0,
            store=store,
        )
        try:
            record = m.submit({"record": point}, key="same").result(10)
            stats = m.inspect("stats").result(5)
            job = m.code.entries["main"].region.job
        finally:
            m.stop()
        assert record.ok and record.outputs == {"value": 42}
        assert not record.stats.get("weak_digests")
        assert conform(job, record, {"record": point}).ok
        results.append(stats)
    assert results[0]["store_misses"] == 1
    assert results[1]["store_hits"] == 1 and results[1]["store_misses"] == 0


# The branch depends on the completed record update, leaving a real snapshot boundary.
SNAPSHOT = """@use "recordtest" as r
@def ()main(point):
    (0)r.Point.make(point)
    ()jump(`loop`)
    loop:
        (point)r.checkpoint(x)
        (x, 1)add(next)
        (point, next)r.Point.with.x(point)
        (point)r.Point.get.x(current)
        (current, 6)less(more)
        (more)branch(`again`, `done`)
        again:
            ()jump(`loop`)
        done:
"""


@pytest.mark.parametrize("core", ["python", "rust"])
def test_records_survive_real_job_snapshot_and_resume(core, monkeypatch):
    store = MemoryStore()
    records = []
    for fail in (True, False):
        monkeypatch.setattr(record_nodes, "FAIL", fail)
        record_nodes.CALLS.clear()
        m = machine(
            SNAPSHOT,
            core=core,
            mode="thread",
            store=store,
            snapshot_every=1,
            window=1,
            retire_buffer=1,
            run_ahead=False,
        )
        try:
            record = m.submit({}, key="snapshot").result(15)
            job = m.code.entries["main"].region.job
        finally:
            m.stop()
        records.append(record)
        if fail:
            assert not record.ok and record.stats.get("snapshots")
            assert len(store) > 0
        else:
            assert record.ok and record.stats.get("resumed"), record.failure
            assert project(POINT, record.outputs["point"], "x") == 6
            assert record_nodes.CALLS and 0 < record_nodes.CALLS[0] <= 3
            assert record_nodes.CALLS[-1] == 5
            assert conform(job, record, {}).ok
    assert not records[0].stats.get("snapshot_error")
    assert len(store) == 0


class JsonEchoEngine(EngineBase):
    """Generic transport fixture: it handles JSON, not record schemas or Python attributes."""

    protocol = "http"
    shares_memory = False

    def __init__(self):
        super().__init__("json-echo")
        self.requests = []

    def capacity(self):
        return 1

    def submit(self, item, done):
        request = item.payload()
        encoded = json.dumps(request.json, allow_nan=False)
        self.requests.append(encoded)
        response = httpx.Response(
            200, json=json.loads(encoded), request=httpx.Request("POST", "http://echo/")
        )
        try:
            result = Ok(item.finish(response), True)
        except Exception as exc:  # noqa: BLE001
            result = ItemError(exc)
        done("echo", [result])
        return "echo"


@pytest.mark.parametrize("core", ["python", "rust"])
def test_explicit_json_adapter_boundary_requires_no_engine_record_support(core):
    program = """@use "recordtest" as r
@def (record: recordtest.Envelope)main(value):
    (record)r.echo(value)
"""
    engine = JsonEchoEngine()
    adapter = JsonHttp(
        "/echo", build=record_nodes.encode_request, parse=record_nodes.decode_response
    )
    value = construct(ENVELOPE, point=construct(POINT, x=4, tags=[1]), labels={"a": [2]})
    m = Machine(
        program=program,
        entry="main",
        libraries=[LIB],
        bind={"recordtest.echo": Use(engine, adapter=adapter)},
        core=core,
        record=1.0,
    )
    try:
        record = m.submit({"record": value}, key="wire").result(10)
        job = m.code.entries["main"].region.job
    finally:
        m.stop()
    assert record.ok and record.outputs == {"value": value}, record.failure
    assert len(engine.requests) == 1
    assert json.loads(engine.requests[0])["schema"] == "recordtest.Envelope"
    assert conform(job, record, {"record": value}, mode="reexecute", pure=lambda _: True).ok

    # A remote response cannot select an arbitrary class or undeclared schema.
    payload = encode_record(value)
    payload["schema"] = "untrusted.Class"
    response = httpx.Response(200, json=payload, request=httpx.Request("POST", "http://echo/"))
    with pytest.raises(ValueError, match="schema"):
        adapter.decode(response, "http")


@pytest.mark.parametrize("core", ["python", "rust"])
def test_immutable_input_shared_across_jobs_passes_alias_checks(core):
    program = """@use "recordtest" as r
@def (record: recordtest.Envelope)main(value):
    (record)r.echo(value)
"""
    value = construct(ENVELOPE, point=construct(POINT, x=3))
    m = machine(program, core=core, mode="thread")
    sink = Memory()
    try:
        m.run(Iterable([SourceItem(str(i), {"record": value}) for i in range(8)]), sink)
    finally:
        m.stop()
    assert len(sink.records) == 8
    assert all(record.ok and record.outputs == {"value": value} for record in sink.records)
