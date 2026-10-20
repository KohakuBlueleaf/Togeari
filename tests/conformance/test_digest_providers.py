"""Conformance does not depend on what the replaying process imported: each run names the
modules that register digests, and ``togeari conform`` imports them before replaying."""

import json
import os
import subprocess
import sys
from pathlib import Path

import blobs.digests
import blobs.nodes
from blobs.kinds import Blob

from conftest import bounded
from togeari import Lane, Machine, SourceItem, Use
from togeari.io import Iterable, Memory
from togeari.plugins import Archive, Recorder
from togeari.testing import SimCall

HERE = Path(__file__).parent
SIZE = "@def (b)main(n):\n    (b)size(n)\n"
WEAK = "@def (x: int)main(n):\n    (x)guard(lock)\n    (lock)held(n)\n"


def record(out: Path, program: str, inputs: list[dict]) -> None:
    lane = Lane([SimCall(name="c", latency_ms=0.2)])
    bind = {name: Use(lane) for name in ("size", "guard", "held")}
    m = Machine(
        program=program,
        entry="main",
        libraries=[blobs.nodes.LIB],
        bind=bind,
        record=1.0,
        plugins=[Recorder(out)],
    )
    jobs = [SourceItem(str(k), x) for k, x in enumerate(inputs)]
    try:
        bounded(m, lambda: m.run(Iterable(jobs), Memory()), 30)
    finally:
        m.stop()


def conform_fresh(out: Path) -> subprocess.CompletedProcess:
    """``togeari conform`` in a fresh interpreter that has only the test folder on its path."""
    code = "import sys; from togeari.cli.main import main; sys.exit(main(sys.argv[1:], sys.stdout))"
    env = {**os.environ, "PYTHONPATH": str(HERE)}
    return subprocess.run(
        [sys.executable, "-c", code, "conform", str(out)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def test_a_registered_digest_replays_in_a_fresh_interpreter(tmp_path):
    out = tmp_path / "rec"
    record(out, SIZE, [{"b": Blob(bytes([k]) * (k + 1))} for k in range(3)])

    # The run names the module that registered the digest
    archive = Archive(out)
    [run] = archive.runs()
    assert blobs.digests.__name__ in archive.run_info(run)["digest_providers"]

    # A fresh interpreter replays it: input digests match the record
    res = conform_fresh(out)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "3/3 jobs conform" in res.stdout


def test_conform_prints_weak_digest_counts(tmp_path):
    out = tmp_path / "rec"
    record(out, WEAK, [{"x": 1}])
    [entry] = Archive(out).jobs()
    assert entry["stats"]["weak_digests"] == {"lock": 1}

    # A lock replays as its repr: the job fails, and the counts are printed
    res = conform_fresh(out)
    [run] = Archive(out).runs()
    assert res.returncode == 1
    assert "weak digests lock x1" in res.stdout, res.stdout
    assert f"run {run}: 0/1 conform; 1 with weak values; 1 weak digests" in res.stdout
    assert "0/1 jobs conform; 1 with weak values; 1 weak digests" in res.stdout
    assert json.loads((out / "runs" / run / "run.json").read_text())["run"] == run
