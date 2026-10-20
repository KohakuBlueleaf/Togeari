"""Run report, trace and recorder: every file is written, parseable, and complete."""

import json
import threading

import pytest
from kohakunode.testing import LIB
from plugin_programs import PROGRAM, jobs, read_jsonl

from conftest import build, run_jobs
from togeari.cli.loading import resolve
from togeari.cli.replay import replay_dir
from togeari.plugins import Archive, Counters, Perfetto, Recorder, RunReport
from togeari.plugins.common.worker import Worker
from togeari.plugins.record.archive import ArchiveWriter

try:
    import zstandard
except ImportError:
    zstandard = None


def replay_archive(path) -> list:
    """(job key, verdict) of every archived job, replayed from the files alone."""
    return [(r.key, r.verdict) for r in replay_dir(path)]


@pytest.mark.parametrize("shared_counters", [False, True])
def test_run_report_writes_every_file(tmp_path, shared_counters):
    out = tmp_path / "report"
    plugins = [RunReport(out, compress=False, interval_s=0.05)]
    if shared_counters:
        plugins.append(Counters(interval_s=0.05))
    # Calls of 10 ms keep the run many snapshot intervals long on any runner
    m = build(PROGRAM, "main", n_units=2, latency_ms=10.0, plugins=plugins, events="all")
    try:
        run_jobs(m, jobs(25))
        calls = m.dispatcher.stats["calls"]
    finally:
        m.stop()

    # spec.json and env.json describe the machine and its environment
    spec = json.loads((out / "spec.json").read_text())
    assert "main" in spec["entries"]
    assert len(spec["programs"]["0"]["semantic_hash"]) == 64
    assert spec["units"]["call0"] == {"engine": "sim", "name": "call0", "kind": "call"}
    assert {ln["name"] for ln in spec["lanes"]} == {"call", "batch"}
    assert spec["bindings"]["inc"]["type"] == "Use"
    assert spec["knobs"]["record"] == 1.0
    env = json.loads((out / "env.json").read_text())
    assert env["packages"]["kohakunode"]
    assert "call0" in env["units"]

    # metrics.jsonl: snapshots that add up to the run
    metrics = read_jsonl(out / "metrics.jsonl")
    assert len(metrics) >= 2
    assert sum(s["jobs"]["ended"] for s in metrics) == 25
    assert sum(sum(s["work"]["calls_by_node"].values()) for s in metrics) == calls

    # events.jsonl: every event, since events="all"
    events = read_jsonl(out / "events.jsonl")
    kinds = [e["kind"] for e in events]
    assert kinds.count("job.admit") == 25
    assert kinds.count("job.end") == 25
    assert kinds.count("stmt.fetch") == calls

    # summary.json and the conformance records
    summary = json.loads((out / "summary.json").read_text())
    assert summary["jobs"] == 25
    assert summary["records"] == 25
    assert summary["event_lines"] == len(events)
    assert summary["metric_lines"] == len(metrics)
    verdicts = replay_archive(out)
    assert len(verdicts) == 25
    assert all(v.ok for _, v in verdicts), verdicts


@pytest.mark.skipif(zstandard is None, reason="zstandard is not installed")
def test_run_report_compressed_events(tmp_path):
    out = tmp_path / "report"
    m = build(PROGRAM, "main", plugins=[RunReport(out, compress=True, interval_s=0.05)])
    try:
        run_jobs(m, jobs(10))
    finally:
        m.stop()
    with (out / "events.jsonl.zst").open("rb") as f:
        text = zstandard.ZstdDecompressor().stream_reader(f).read().decode()
    kinds = [json.loads(line)["kind"] for line in text.splitlines() if line]
    assert kinds.count("job.end") == 10


def test_perfetto_trace_has_every_track(tmp_path):
    path = tmp_path / "trace.json"
    plugins = [Perfetto(path, sample_s=0.05)]
    m = build(PROGRAM, "main", n_units=2, latency_ms=1.0, plugins=plugins, events="all")
    try:
        run_jobs(m, jobs(15))
        units = {u.name for ln in m.lanes for u in ln.units}
        submitted = sum(ln.stats["submitted"] for ln in m.lanes)
    finally:
        m.stop()
    trace = json.loads(path.read_text())

    # Processes, a begin and an end per job, counter tracks per phase, lane and unit
    procs = {e["args"]["name"] for e in trace if e.get("name") == "process_name"}
    assert procs == {"dispatcher", "lanes", "units", "jobs"}
    begins = {e["id"] for e in trace if e.get("ph") == "b"}
    ends = {e["id"] for e in trace if e.get("ph") == "e"}
    assert len(begins) == 15 and begins == ends
    counters = {e["name"] for e in trace if e.get("ph") == "C"}
    assert {"queue call", "queue batch"} <= counters
    assert any(n.startswith("phase ") for n in counters)
    assert {f"unit {u}" for u in units} <= counters
    threads = {e["args"]["name"] for e in trace if e.get("name") == "thread_name"}
    assert threads and threads <= units

    # A service slice per answered item, on its unit's thread
    slices = [e for e in trace if e.get("ph") == "X"]
    assert len(slices) == submitted
    assert all(e["dur"] >= 0 for e in slices)


def test_recorder_records_replay_and_catch_tampering(tmp_path):
    out = tmp_path / "rec"
    m = build(PROGRAM, "main", plugins=[Recorder(out)])
    try:
        run_jobs(m, jobs(12))
    finally:
        m.stop()

    # Every job replays from the files
    verdicts = replay_archive(out)
    assert len(verdicts) == 12
    assert all(v.ok for _, v in verdicts)
    [file] = (out / "programs").glob("*.json")
    program = json.loads(file.read_text())
    assert program["entry"] == "main" and len(program["hash"]) == 64
    assert file.stem == program["hash"]
    [ref] = program["libraries"]
    assert ref.startswith("kohakunode.") and resolve(ref) is LIB

    # A changed recorded result no longer conforms
    archive = Archive(out)
    entries = list(archive.jobs())
    entries[0]["outputs"] = {"result": "tampered"}
    archive.rewrite(entries[:1])
    bad = dict(replay_archive(out))[entries[0]["key"]]
    assert not bad.ok and bad.reason == "outputs differ"


PROGRAM_V2 = PROGRAM.replace("z = 7", "z = 8")


def test_recorder_keeps_each_run_with_its_own_program(tmp_path):
    out = tmp_path / "rec"
    for program in (PROGRAM, PROGRAM_V2):
        m = build(program, "main", plugins=[Recorder(out)])
        try:
            run_jobs(m, jobs(6))
        finally:
            m.stop()

    # Two runs, two programs under one entry name; every job replays against its own.
    archive = Archive(out)
    assert len(archive.runs()) == 2
    assert len(archive.programs) == 2
    entries = list(archive.jobs())
    assert len(entries) == 12 and len({e["program"] for e in entries}) == 2
    verdicts = replay_archive(out)
    assert len(verdicts) == 12 and all(v.ok for _, v in verdicts), verdicts

    # One run alone
    first = archive.runs()[0]
    assert len([r for r in replay_dir(out, runs=[first])]) == 6


def test_two_writers_of_one_directory_keep_every_program(tmp_path):
    out = tmp_path / "rec"
    writers = [ArchiveWriter(out), ArchiveWriter(out)]
    want = {}
    for i in range(40):
        for w, writer in enumerate(writers):
            info = {"hash": f"{w}-{i:03d}", "entry": "main", "source": f"w{w}"}
            want[info["hash"]] = info
            writer.add_program(info)
    for writer in writers:
        writer.close()

    # Every program of both writers is read back, each as written
    assert Archive(out).programs == want
    assert len(Archive(out).runs()) == 2


def test_worker_queue_is_bounded_and_counts_drops():
    gate = threading.Event()
    entered = threading.Event()
    seen = []

    def handle(task):
        entered.set()
        gate.wait(10.0)
        seen.append(task)

    w = Worker("bounded", handle, maxsize=2)
    try:
        assert w.put(0)
        assert entered.wait(5.0)
        assert [w.put(i) for i in (1, 2, 3, 4)] == [True, True, False, False]
        assert w.dropped == 2
    finally:
        gate.set()
        w.close()
    assert seen == [0, 1, 2]


def test_recorder_without_record_knob_keeps_nothing(tmp_path):
    out = tmp_path / "rec"
    m = build(PROGRAM, "main", record=None, plugins=[Recorder(out)])
    try:
        sink = run_jobs(m, jobs(5))
    finally:
        m.stop()
    assert len(sink.records) == 5
    assert list(Archive(out).jobs()) == []
