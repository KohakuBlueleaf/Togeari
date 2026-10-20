"""Job states in the counters, weak counts and a summary written while the run goes."""

import json
import threading
import time
from types import SimpleNamespace

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.core.plugin.base import JOB_STATES, job_state
from togeari.core.runtime.state import InfState, StrandState
from togeari.plugins import Counters, RunReport
from togeari.testing import SimCall

LIB = NodeLibrary("progress")


@LIB.node()
def inc(x: int) -> int:
    return x + 1


@LIB.node(outputs=["lk"])
def mklock(x: int) -> object:
    return threading.Lock()


@LIB.node()
def held(lk: object) -> int:
    return 1


INC = "@def (x: int)main(y):\n    (x)inc(y)\n"
LOCKED = "@def (x: int)main(y, lk):\n    (x)mklock(lk)\n    (lk)held(y)\n"


def wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def slow_lane(capacity: int) -> Lane:
    return Lane([SimCall(name="slow", latency_ms=1500, tail=0.0, capacity=capacity)], name="s")


# ---------------------------------------------------------------- job states
def fake_job(paused=False, issued=0, window=(), unfinished=0, runnable=(), strands=()):
    return SimpleNamespace(
        paused=paused,
        issued=issued,
        window=list(window),
        unfinished=unfinished,
        runnable=list(runnable),
        strands={i: SimpleNamespace(state=s) for i, s in enumerate(strands)},
    )


def issued(sent: bool) -> SimpleNamespace:
    ext = {"sent": 1.0} if sent else {}
    return SimpleNamespace(state=InfState.ISSUED, item=SimpleNamespace(ext=ext))


def test_each_job_shape_has_its_state():
    cases = {
        "parked": [
            fake_job(paused=True, issued=1, window=[issued(True)]),
            fake_job(strands=[StrandState.PARKED]),
        ],
        "executing": [fake_job(issued=2, window=[issued(False), issued(True)])],
        "queued": [fake_job(issued=1, window=[issued(False)])],
        "waiting": [
            fake_job(unfinished=1, strands=[StrandState.PARKED]),
            fake_job(runnable=[object()]),
            fake_job(strands=[StrandState.STALLED]),
        ],
    }
    assert set(cases) == set(JOB_STATES)
    for state, jobs in cases.items():
        assert [job_state(j) for j in jobs] == [state] * len(jobs)


def test_counters_split_resident_jobs_by_state():
    lane = slow_lane(capacity=1)
    counters = Counters(interval_s=0.05, emit=False)
    m = Machine(
        program=INC, entry="main", libraries=[LIB], bind={"inc": Use(lane)}, plugins=[counters]
    )
    try:
        handles = [m.submit({"x": i}) for i in range(3)]

        # One call on the unit, two in the lane queue; then one queued job paused
        assert wait_for(lambda: lane.signals()["queue"] == 2)
        jobs = m.inspect("jobs").result(5)
        queued = [j for j in jobs if j["id"] != _executing_id(m)]
        assert m.command("pause_job", id=queued[0]["id"]).result(5)

        def settled():
            snap = m.inspect("counters").result(5).get("jobs", {})
            return snap.get("parked") == 1 and snap.get("executing") == 1

        assert wait_for(settled)
        snap = m.inspect("counters").result(5)["jobs"]
        assert (snap["executing"], snap["queued"], snap["waiting"], snap["parked"]) == (1, 1, 0, 1)
        assert sum(snap[s] for s in JOB_STATES) == snap["resident"] == 3
        assert snap["job_seconds"]["executing"] > 0
        m.command("resume_job", id=queued[0]["id"]).result(5)
        assert [h.result(10).outputs["y"] for h in handles] == [1, 2, 3]
    finally:
        m.stop(drain=False, timeout=5)


def _executing_id(m) -> int:
    for job in m.dispatcher.jobs.values():
        if job_state(job) == "executing":
            return job.id
    return -1


# ---------------------------------------------------------------- summary.json
def test_summary_json_is_written_while_jobs_are_resident(tmp_path):
    out = tmp_path / "report"
    lane = slow_lane(capacity=8)
    m = Machine(
        program=INC,
        entry="main",
        libraries=[LIB],
        bind={"inc": Use(lane)},
        plugins=[RunReport(out, compress=False, interval_s=0.05)],
    )
    try:
        handles = [m.submit({"x": i}) for i in range(4)]
        assert wait_for(lambda: (out / "summary.json").exists())

        # Before any job ends: a whole, parseable summary of the run so far
        assert m.inspect("stats").result(5)["jobs_ok"] == 0
        early = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        assert early["jobs"] == 0 and early["seconds"] > 0
        assert [h.result(10).ok for h in handles] == [True] * 4
    finally:
        m.stop()

    # After stop: the final summary is whole and no temporary file is left
    final = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert final["jobs"] == 4 and final["seconds"] >= early["seconds"]
    assert not list(out.glob("*.tmp"))


def test_summary_counts_weak_digests_and_weak_values(tmp_path):
    out = tmp_path / "report"
    m = Machine(
        program=LOCKED,
        entry="main",
        libraries=[LIB],
        bind={"mklock": Inline(), "held": Inline()},
        plugins=[RunReport(out, compress=False, interval_s=0.05)],
        record=1.0,
    )
    try:
        assert all(m.submit({"x": i}).result(5).ok for i in range(3))
    finally:
        m.stop()
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))

    # `held` digests its lock input by type; each job's lock output is stored by repr
    assert summary["weak_digests"] == {"lock": 3}
    assert summary["weak_digest_count"] == 3
    assert summary["weak_jobs"] == 3 and summary["weak_values"] >= 3


def test_summary_without_weak_values_says_zero(tmp_path):
    out = tmp_path / "report"
    m = Machine(
        program=INC,
        entry="main",
        libraries=[LIB],
        bind={"inc": Inline()},
        plugins=[RunReport(out, compress=False, interval_s=0.05)],
        record=1.0,
    )
    try:
        assert m.submit({"x": 1}).result(5).ok
    finally:
        m.stop()
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert (summary["weak_digest_count"], summary["weak_jobs"], summary["weak_values"]) == (0, 0, 0)
