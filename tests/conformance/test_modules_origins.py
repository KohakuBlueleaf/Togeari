"""Linked modules, source provenance and portable archives on both execution cores."""

import copy
import hashlib
import json
import shutil
import urllib.request
from dataclasses import replace

import pytest
from kohakunode.lower import semantic_hash
from kohakunode.sig.std import STD
from kohakunode.syntax.ast import Origin, walk

from togeari import Inline, Machine, SourceItem
from togeari.cli.replay import job_program, replay_dir
from togeari.conformance import conform
from togeari.core.construct.errors import Problems
from togeari.core.program.decode.compiler import Compiler
from togeari.core.program.decode.instr import Op
from togeari.core.program.job import build_job_programs
from togeari.core.program.origin import program_origins, restore_origins
from togeari.io import Iterable, Memory
from togeari.plugins import Archive, Inspector, Recorder
from togeari.plugins.record.archive import program_info

MAIN = """@import "left.kir" as left
@import "right.kir" as right
@def (x: int)main(y: int):
    (x)left.run(a)
    (a)left.run(b)
    (b)right.run(y)
"""
LEFT = """@use "kir.std" as host
@export run
@def (x: int)helper(y: int):
    (x, 1)host.add(y)
@def (x: int)run(y: int):
    (x)helper(y)
"""
RIGHT = """@use "kir.std" as host
@export run
@def (x: int)helper(y: int):
    (x, 2)host.mul(y)
@def (x: int)run(y: int):
    (x)helper(y)
"""


def project(folder, main=MAIN, left=LEFT, right=RIGHT):
    folder.mkdir(parents=True)
    for name, text in (("main.kir", main), ("left.kir", left), ("right.kir", right)):
        (folder / name).write_text(text, encoding="utf-8")
    return folder / "main.kir"


def build_program(path):
    problems = Problems()
    jobs = build_job_programs(path, ["main"], [], None, None, problems)
    problems.raise_if_any()
    return jobs[0]


def execute(path, core, plugins=(), x=3, **knobs):
    machine = Machine(
        program=path,
        entry="main",
        bind={"kir.std.*": Inline()},
        core=core,
        record=1.0,
        plugins=list(plugins),
        **knobs,
    )
    sink = Memory()
    try:
        machine.run(Iterable([SourceItem("job", {"x": x})]), sink)
    finally:
        machine.stop()
    return machine, sink.records[0]


@pytest.mark.parametrize("core", ["python", "rust"])
def test_private_collisions_repeated_inline_and_conformance(tmp_path, core):
    path = project(tmp_path / "source")
    machine, record = execute(path, core)
    job = machine.code.entries["main"].region.job
    assert record.ok and record.outputs == {"y": 10}
    assert conform(job, record, {"x": 3}).ok
    assert conform(job, record, {"x": 3}, mode="reexecute", pure=lambda _: True).ok
    assert job.source_program.imports and not job.written.imports
    assert job.dependencies["version"] == 1

    # Same private definition and source node, different nested call ancestry.
    origins = record.stats["record_origins"]
    assert len(origins) == len(record.record) == 3
    first, second, third = origins
    assert first["module"] == second["module"] == "left.kir"
    assert first["node_id"] == second["node_id"]
    assert first["definition"] == second["definition"] == "helper"
    assert first["calls"] != second["calls"]
    assert len(first["calls"]) >= 2
    assert third["module"] == "right.kir" and third["definition"] == "helper"
    assert all(len(call) == 5 for call in record.record)
    assert all(not stmt.meta for stmt in walk(job.program.body))
    assert "left.kir" in machine.code.dump(machine.code.entries["main"].region)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_fault_origin_keeps_handler_error_contract(tmp_path, core):
    right = RIGHT.replace("(x, 2)host.mul(y)", "(x, 0)host.floordiv(y)")
    machine, record = execute(project(tmp_path / "source", right=right), core)
    assert not record.ok and record.failure["kind"] == "ZeroDivisionError"
    assert set(record.failure) == {"kind", "node", "message"}
    origin = record.stats["fault_origin"]
    assert origin["module"] == "right.kir" and origin["definition"] == "helper"
    assert origin["line"] == 4 and origin["calls"]
    assert record.stats["record_origins"][-1] == origin
    assert conform(machine.code.entries["main"].region.job, record, {"x": 3}).ok


@pytest.mark.parametrize("core", ["python", "rust"])
def test_recording_replays_without_module_files(tmp_path, core):
    path = project(tmp_path / "source")
    archive_path = tmp_path / "archive"
    machine, record = execute(path, core, [Recorder(archive_path)])
    original = machine.code.entries["main"].region.job
    info = program_info(original, [], None)
    assert info["schema_version"] == 2
    assert "@import" not in info["text"] and "@import" in info["authored_text"]
    assert info["origins"]["lowered"]["version"] == 1

    # Neither mutation nor deletion of source dependencies can change replay.
    path.with_name("left.kir").write_text("invalid replacement", encoding="utf-8")
    shutil.rmtree(path.parent)
    rebuilt = job_program(json.loads(json.dumps(info)), [])
    assert rebuilt.semantic_hash == original.semantic_hash
    assert rebuilt.dependencies == original.dependencies
    assert program_origins(rebuilt.program) == program_origins(original.program)
    assert conform(rebuilt, record, {"x": 3}).ok
    verdicts = list(replay_dir(archive_path))
    assert len(verdicts) == 1 and verdicts[0].verdict.ok
    [archived] = Archive(archive_path).jobs()
    assert archived["stats"]["record_origins"] == record.stats["record_origins"]


@pytest.mark.parametrize("core", ["python", "rust"])
def test_nested_import_branch_loop_and_handler_conformance(tmp_path, core):
    main = """@import "left.kir" as left
@def (x: int)main(y: int):
    (x)left.run(y)
"""
    left = """@import "math.kir" as math
@export run
@def (x: int)run(y: int):
    (x)math.total(y)
"""
    math = """@export total
@def (x: int)total(y: int):
    y = 0
    (x)range(xs)
    (xs)each(i, `body`)
    body:
        (i, 2)less(small)
        (small)branch(`low`, `high`)
        low:
            (y, 1)add(y)
        high:
            (y, 2)add(y)
"""
    path = project(tmp_path / "source", main=main, left=left)
    path.with_name("math.kir").write_text(math, encoding="utf-8")
    machine, record = execute(path, core)
    assert record.ok and record.outputs == {"y": 4}
    assert conform(machine.code.entries["main"].region.job, record, {"x": 3}).ok
    origins = record.stats["record_origins"]
    assert all(origin["module"] == "math.kir" for origin in origins)
    assert {call["module"] for call in origins[0]["calls"]} == {"main.kir", "left.kir"}

    # A caught failure remains an ordinary KIR error value, not a provenance envelope.
    main = """@import "right.kir" as right
@def (x: int)main(y):
    @try:
        (x)right.run(y)
    @except error:
        y = error
"""
    right = RIGHT.replace("(x, 2)host.mul(y)", "(x, 0)host.floordiv(y)")
    path = project(tmp_path / "caught", main=main, right=right)
    machine, record = execute(path, core)
    assert record.ok and set(record.outputs["y"]) == {"kind", "node", "message"}
    assert conform(machine.code.entries["main"].region.job, record, {"x": 3}).ok
    assert record.stats["fault_origin"]["module"] == "right.kir"


def test_archive_versions_and_manifest_tampering(tmp_path):
    job = build_program(project(tmp_path / "source"))
    info = program_info(job, [], None)
    info["dependencies"] = copy.deepcopy(info["dependencies"])
    info["dependencies"]["modules"][0]["module"] = "tampered.kir"
    with pytest.raises(ValueError, match="manifest hash differs"):
        job_program(info, [])
    info["schema_version"] = 999
    with pytest.raises(ValueError, match="schema version"):
        job_program(info, [])
    with pytest.raises(ValueError, match="sidecar version"):
        restore_origins(job.program, {"version": 999})

    # A self-consistent manifest still cannot substitute a different host contract.
    info = copy.deepcopy(program_info(job, [], None))
    info["dependencies"]["signatures"][0]["hash"] = "changed"
    manifest = info["dependencies"]
    payload = {key: value for key, value in manifest.items() if key != "hash"}
    manifest["hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="signature contract differs"):
        job_program(info, [])


@pytest.mark.parametrize("change", ["default", "ordered", "doc"])
def test_replay_checks_loaded_signature_contracts(tmp_path, monkeypatch, change):
    job = build_program(project(tmp_path / "source"))
    info = program_info(job, [], None)
    signature = STD.signatures["add"]
    match change:
        case "default":
            first, second = signature.inputs
            altered = replace(signature, inputs=(first, replace(second, default=77)))
        case "ordered":
            altered = replace(signature, ordered=True)
        case "doc":
            altered = replace(signature, doc="Documentation-only revision")
    monkeypatch.setitem(STD.signatures, "add", altered)
    if change == "doc":
        assert job_program(info, []).semantic_hash == job.semantic_hash
    else:
        with pytest.raises(ValueError, match="signature contract differs"):
            job_program(info, [])


def test_archive_preserves_each_runs_metadata_for_one_semantic_hash(tmp_path):
    archive_path = tmp_path / "records"
    originals = {}
    for name, left in (("first", LEFT), ("second", "\n" + LEFT)):
        path = project(tmp_path / name, left=left)
        machine, _ = execute(path, "python", [Recorder(archive_path)])
        job = machine.code.entries["main"].region.job
        originals[job.source] = job
    assert len({job.semantic_hash for job in originals.values()}) == 1
    archive = Archive(archive_path)
    assert len(archive.programs) == 1 and len(archive.runs()) == 2
    for recorded in archive.jobs():
        info = archive.program(recorded["program"], recorded["run"])
        rebuilt = job_program(info, [])
        assert rebuilt.origins == originals[rebuilt.source].origins
    assert all(result.verdict.ok for result in replay_dir(archive_path))

    # Archives from before run-local program metadata use the global index.
    for run in archive.runs():
        shutil.rmtree(archive_path / "runs" / run / "programs")
    assert all(result.verdict.ok for result in replay_dir(archive_path))


@pytest.mark.parametrize("core", ["python", "rust"])
def test_same_semantics_distinct_regions_keep_origins_in_one_run(tmp_path, core):
    first = '@def (x: int)main(y):\n    @meta node_id="first"\n    (x, 1)add(y)\n'
    second = first.replace('node_id="first"', 'node_id="second"')
    archive_path = tmp_path / "records"
    machine = Machine(
        program=first,
        entry="main",
        bind={"kir.std.*": Inline()},
        core=core,
        record=1.0,
        plugins=[Recorder(archive_path)],
    )
    try:
        [new_entry] = machine.load(second, ["main"])
        old, new = machine.code.regions
        assert old.semantic_hash == new.semantic_hash
        assert machine.code.cached(new.job) is new
        assert machine.code.cached(old.job) is None
        views = machine.inspect("programs").result(5)
        assert len(views) == 2
        assert {
            origin["node_id"]
            for view in views
            for origin in view["origins"].values()
            if origin is not None
        } == {"first", "second"}
        records = [
            machine.submit({"x": 3}, entry=entry, key=str(index)).result(5)
            for index, entry in enumerate(("main", new_entry))
        ]
        assert [r.stats["program_region"] for r in records] == [old.index, new.index]
        assert [r.stats["record_origins"][0]["node_id"] for r in records] == ["first", "second"]
    finally:
        machine.stop()
    archive = Archive(archive_path)
    [run] = archive.runs()
    for index, node_id in ((old.index, "first"), (new.index, "second")):
        info = archive.program(old.semantic_hash, run, index)
        rebuilt = job_program(info, [])
        assert rebuilt.program.body[0].origin.node_id == node_id
    assert all(result.verdict.ok for result in replay_dir(archive_path))


def test_relocation_and_dependency_identity(tmp_path):
    first = build_program(project(tmp_path / "one"))
    second = build_program(project(tmp_path / "two"))
    assert first.semantic_hash == second.semantic_hash
    assert first.dependencies == second.dependencies
    assert first.origins == second.origins
    assert str(tmp_path) not in json.dumps(first.dependencies)
    changed = build_program(project(tmp_path / "three", right=RIGHT.replace("x, 2", "x, 3")))
    assert changed.semantic_hash != first.semantic_hash
    assert changed.dependencies["hash"] != first.dependencies["hash"]


def test_origins_do_not_change_semantic_identity_and_legacy_replay():
    text = "@def (x: int)main(y: int):\n    (x, 1)add(y)\n"
    job = build_program(text)
    assert job.dependencies == {}
    expected = hashlib.sha256(
        "\x1f".join((semantic_hash(job.program), "main", ",".join(job.passes))).encode()
    ).hexdigest()
    assert job.semantic_hash == expected
    changed = copy.deepcopy(job.program)
    for stmt in walk(changed.body):
        stmt.origin = Origin("elsewhere.kir", "editor-node", line=900)
    assert semantic_hash(changed) == semantic_hash(job.program)
    sidecar = json.loads(json.dumps(program_origins(job.program)))
    restore_origins(changed, sidecar)
    assert program_origins(changed) == program_origins(job.program)

    # Old program JSON has no version, dependency or provenance fields.
    info = program_info(job, [], None)
    for key in ("schema_version", "authored_text", "dependencies", "origins"):
        info.pop(key)
    assert job_program(info, []).semantic_hash == job.semantic_hash


def test_decode_prefers_explicit_origin_over_generated_spelling():
    job = build_program("@def (x: int)main(y: int):\n    (x, 1)add(y)\n")
    stmt = job.program.body[0]
    stmt.origin = Origin("real.kir", "stable-id", 42, "actual_definition")
    stmt.outs = ["misleading__17__value"]
    compiler = Compiler([], 0, job.env, "wrong.kir")
    span = compiler.span(stmt)
    assert (span.source, span.line, span.def_of) == ("real.kir", 42, "actual_definition")
    assert span.origin.node_id == "stable-id"
    stmt.origin = None
    assert compiler.span(stmt).def_of == "misleading"


def test_generated_iteration_instruction_origin():
    text = """@def (x: int)main(y: int):
    y = 0
    (x)range(xs)
    (xs)each(item, `body`)
    body:
        (y, item)add(y)
"""
    job = build_program(text)
    compiler = Compiler([], 0, job.env, "<text>")
    compiler.compile(job.program.body)
    [each] = [ins for ins in compiler.mem if ins.op == Op.EACH]
    [end] = [ins for ins in compiler.mem if ins.op == Op.ITER_END]
    assert end.span.origin == replace(each.span.origin, generated="iter_end")
    legacy_origins = []
    each.record_origin(legacy_origins, 2)
    assert legacy_origins == [None, None, each.origin_dict()]


@pytest.mark.parametrize("core", ["python", "rust"])
def test_inspector_program_origins_are_json_and_detached(tmp_path, core):
    inspector = Inspector(token=None)
    machine = Machine(
        program=project(tmp_path / "source"),
        entry="main",
        bind={"kir.std.*": Inline()},
        core=core,
        plugins=[inspector],
    )
    try:
        with urllib.request.urlopen(inspector.url + "/queries/programs", timeout=5) as response:
            [program] = json.load(response)
        origins = [o for o in program["origins"].values() if o and o["module"] == "left.kir"]
        assert origins and origins[0]["calls"]
        program["dependencies"].clear()
        [fresh] = machine.inspect("programs").result(5)
        assert fresh["dependencies"]["version"] == 1
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_record_limit_drops_origin_sidecar(tmp_path, core):
    _, record = execute(project(tmp_path / "source"), core, record_limit=1)
    assert record.record is None
    assert "record_origins" not in record.stats
