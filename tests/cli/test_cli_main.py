"""The ``togeari`` command line: check, decode, run, conform, units, doctor."""

import importlib
import io
import json
import multiprocessing
import os
import sys
import textwrap
import time

import pytest

from togeari.cli.commands import doctor
from togeari.cli.main import main
from togeari.engines.common import registry
from togeari.engines.python import child as python_child
from togeari.plugins import Archive

PROGRAM = """
@def (a: int, b: int = 3)main(result):
    (a)inc(x)
    (x, b)mix(y)
    (y)small(c)
    (c)branch(`yes`, `no`)
    yes:
        (y)inc(z)
    no:
        z = 7
    (z, x)mix(result)
"""

MACHINE = """
from kohakunode.testing import LIB

from togeari import Inline, Lane, Machine, Use
from togeari.io import Iterable, Memory
from togeari.policies import SizeOrWait
from togeari.testing import SimBatch, SimCall

PROGRAM = {program!r}
SAMPLES = {{
    "call0": [("inc", {{"x": 1}}), ("small", {{"x": 5}})],
    "batch0": [("mix", {{"a": 1, "b": 2}})],
}}


UNITS = []


def unit_samples(machine):
    return dict(SAMPLES)


def build():
    units = [SimCall(name="call0", latency_ms=0.3), SimBatch(name="batch0", latency_ms=0.3)]
    UNITS.extend(units)
    call = Lane([units[0]], name="call")
    batch = Lane([units[1]], former=SizeOrWait(8, 1), name="batch")
    bind = {{"inc": Use(call), "mix": Use(batch), "kir.std.*": Inline()}}
    {extra}
    machine = Machine(program=PROGRAM, entry="main", libraries=[LIB], bind=bind, record=1.0)
    return machine, Iterable([{{"a": i}} for i in range(10)]), Memory()
"""


def machine_file(tmp_path, extra: str = 'bind["small"] = Use(call)') -> str:
    path = tmp_path / "machine.py"
    path.write_text(MACHINE.format(program=PROGRAM, extra=extra), encoding="utf-8")
    return str(path)


def cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out)
    return code, out.getvalue()


def test_check_reports_problems(tmp_path):
    code, out = cli("check", machine_file(tmp_path))
    assert code == 0 and "ok: entries main" in out

    # build() takes no argument; still no unit was started (a started one keeps its t0).
    units = sys.modules["togeari_machine_machine"].UNITS
    assert len(units) == 2 and all(u.t0 == 0.0 for u in units)

    bad = tmp_path / "bad"
    bad.mkdir()
    code, out = cli("check", machine_file(bad, extra="pass"))
    assert code == 1 and "'small' is not bound" in out


def test_decode_prints_tables(tmp_path):
    prog = tmp_path / "prog.kir"
    prog.write_text(textwrap.dedent(PROGRAM), encoding="utf-8")
    code, out = cli("decode", str(prog), "--entry", "main", "--lib", "kohakunode.testing:LIB")
    assert code == 0
    assert out.startswith("region 0")
    assert "(a)inc(x)" in out and "branch" in out and "reconv=" in out

    # An unknown entry is a problem
    code, out = cli("decode", str(prog), "--entry", "nope", "--lib", "kohakunode.testing:LIB")
    assert code == 1 and "'nope' is not a top-level @def" in out


def test_run_with_report_then_conform(tmp_path):
    report = tmp_path / "report"
    code, out = cli("run", machine_file(tmp_path), "--limit", "6", "--report", str(report))
    assert code == 0
    summary = json.loads(out)
    assert summary["jobs"] == 6
    assert json.loads((report / "summary.json").read_text())["jobs"] == 6

    # The recorded run conforms
    code, out = cli("conform", str(report))
    assert code == 0 and "6/6 jobs conform" in out

    # A changed record does not
    archive = Archive(report)
    entries = list(archive.jobs())
    entries[2]["record"][0] = (*entries[2]["record"][0][:3], -12345, None)
    archive.rewrite([entries[2]])
    code, out = cli("conform", str(report))
    assert code == 1 and "5/6 jobs conform" in out and f"FAIL {entries[2]['key']}" in out


def test_conform_reexecute_catches_a_consistent_wrong_result(tmp_path):
    report = tmp_path / "report"
    code, _ = cli("run", machine_file(tmp_path), "--limit", "4", "--report", str(report))
    assert code == 0

    # A wrong final `mix` result, with the job's output changed to match it
    archive = Archive(report)
    entry = list(archive.jobs())[1]
    last = entry["record"][-1]
    assert last[0] == "mix"
    entry["record"][-1] = (*last[:3], -777, None)
    entry["outputs"] = {**entry["outputs"], "result": -777}
    archive.rewrite([entry])

    # Replay sees a consistent record; re-executing `mix` does not
    code, out = cli("conform", str(report))
    assert code == 0 and "4/4 jobs conform" in out
    code, out = cli("conform", str(report), "--reexecute", "mix")
    assert code == 1 and "3/4 jobs conform" in out and f"FAIL {entry['key']}" in out


def test_units_runs_the_engine_checks(tmp_path):
    code, out = cli("units", machine_file(tmp_path), "--rounds", "40")
    assert code == 0, out
    assert "ok   call/call0" in out and "ok   batch/batch0" in out
    assert "2/2 units pass" in out

    # A unit without samples is skipped
    extra = 'bind["small"] = Use(call); SAMPLES.pop("batch0")'
    code, out = cli("units", machine_file(tmp_path, extra=extra), "--rounds", "40")
    assert "skipped batch/batch0: no samples" in out and "1/1 units pass" in out


def test_doctor_reports_versions():
    code, out = cli("doctor")
    assert code == 0
    assert out.startswith("python")
    assert "kohakunode" in out and "devices" in out and "leftover togeari worker" in out


def _register(pid: int, parent: int, entry: str = doctor.WORKER_ENTRIES[0]) -> None:
    info = {"pid": pid, "parent": parent, "entry": entry, "started": time.time()}
    (registry.REGISTRY / f"{pid}.json").write_text(json.dumps(info), encoding="utf-8")


SPAWNED = (
    "python -c from multiprocessing.spawn import spawn_main; spawn_main() --multiprocessing-fork"
)


def test_doctor_flags_only_leftover_worker_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY", tmp_path)
    parent, leftover, live, stranger, plain, gone = 100, 201, 202, 203, 204, 205

    # The process table: a running parent and four children; the leftover's parent is gone
    procs = {
        parent: "python -m pytest",
        leftover: SPAWNED,
        live: SPAWNED,
        stranger: SPAWNED,
        plain: "python -c import time; time.sleep(60) togeari",
    }
    _register(leftover, 999)
    _register(live, parent)
    _register(stranger, 999, entry="somebody.else.child")
    _register(plain, 999)
    _register(gone, parent)

    found, running = doctor.worker_processes(procs)
    assert [w["pid"] for w in found] == [leftover]
    assert [w["pid"] for w in running] == [live]

    # A registered pid the table lacks has its file removed; the others keep theirs
    assert not (tmp_path / f"{gone}.json").exists()
    assert (tmp_path / f"{leftover}.json").exists()


def test_doctor_lists_a_spawned_child_with_its_command_line():
    child = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(60,), daemon=True)
    child.start()
    try:
        procs = doctor.processes()
        assert os.getpid() in procs
        assert "multiprocessing" in procs.get(child.pid, ""), procs.get(child.pid)
    finally:
        child.kill()


def test_doctor_worker_entries_are_the_child_modules():
    pytest.importorskip("torch")
    entries = (python_child.__name__, importlib.import_module("togeari.engines.torch.child"))
    assert (entries[0], entries[1].__name__) == doctor.WORKER_ENTRIES
