"""The telemetry plugin on a running machine, checked against independent counts.

The references: the dispatcher's own totals (calls, jobs), the lanes' submitted counts, the
counters plugin's calls per node, and the exact quantiles of the raw records. Every tier must
agree with the raw records in counts and sums.
"""

import collections
import gc
import math
import os
import threading

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("kohakuvault")

from plugin_programs import PROGRAM, jobs  # noqa: E402

from conftest import build, run_jobs  # noqa: E402
from togeari.core.contracts import telemetry as R  # noqa: E402
from togeari.core.observe.ring import Ring  # noqa: E402
from togeari.core.plugin.base import BasePlugin  # noqa: E402
from togeari.plugins import Counters, Inspector  # noqa: E402
from togeari.plugins.common.hist import bin_of, lower, upper  # noqa: E402
from togeari.plugins.telemetry import Telemetry  # noqa: E402
from togeari.plugins.telemetry import layout as L  # noqa: E402
from togeari.plugins.telemetry.query import Run  # noqa: E402
from togeari.plugins.telemetry.store import Segments  # noqa: E402

CORES = ["python", "rust"]
WRITERS = ["thread", "process"]


def run_machine(tmp_path, core, n=60, clean=True, **tel):
    counters = Counters(interval_s=0.05)
    snaps: list[dict] = []
    counters.watchers.append(snaps.append)
    t = Telemetry(tmp_path / "tel", trace_rate=0.2, **tel)
    m = build(PROGRAM, "main", n_units=2, latency_ms=1.0, plugins=[t, counters], core=core)
    try:
        sink = run_jobs(m, jobs(n))
        stats = m.inspect("stats").result()
        submitted = {ln.name: ln.stats["submitted"] for ln in m.lanes}
        lanes = {ln.name: [u.name for u in ln.units] for ln in m.lanes}
    finally:
        m.stop()
    status = t.status()
    assert status["writer"]["mode"] == tel.get("writer", "process"), status
    assert not clean or status["writer"]["errors"] == 0, status
    assert status["writer"].get("lost") is None, status
    assert status["ring"]["dropped"] == 0, status
    return Run(tmp_path / "tel"), sink, stats, submitted, lanes, snaps


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("core", CORES)
def test_samples_hold_the_dispatcher_job_states_and_unit_capacity(tmp_path, core, writer):
    run, sink, stats, _, lanes, _ = run_machine(
        tmp_path, core, n=200, flush_s=0.02, metrics_s=0.02, writer=writer
    )
    units = [u for us in lanes.values() for u in us]
    names = ["dispatcher/cycles", "dispatcher/busy_s", "dispatcher/calls", "jobs/resident"]
    names += [f"jobs/{s}" for s in ("executing", "queued", "waiting", "parked")]
    names += [
        f"unit/{u}/{k}"
        for u in units
        for k in ("held", "free", "busy_s", "done", "running", "waiting")
    ]
    got = run.samples(names, 0, 1e12)
    assert set(got) == set(names)

    # Cumulative counters never go back; the last sample is taken at stop, after the totals
    # read at the end of the run
    for k in ("cycles", "calls"):
        v = got[f"dispatcher/{k}"]["v"]
        assert v == sorted(v) and v[-1] >= stats[k] > 0, k
    assert all(b >= 0 for b in got["dispatcher/busy_s"]["v"])

    # Each sample's job states add up to its resident jobs
    resident = dict(zip(got["jobs/resident"]["t"], got["jobs/resident"]["v"]))
    for i, t in enumerate(got["jobs/executing"]["t"]):
        states = sum(got[f"jobs/{s}"]["v"][i] for s in ("executing", "queued", "waiting", "parked"))
        assert states == resident[t]

    # One held and one free count per sample, side by side; free is what the engine reported
    for u in units:
        held, free = got[f"unit/{u}/held"], got[f"unit/{u}/free"]
        assert held["t"] == free["t"], u
        assert all(f >= 0 for f in free["v"]) and all(h >= 0 for h in held["v"]), u

    # The dispatcher's time per sample and the feeder's own time are series of their own
    cost = run.samples(["telemetry/sample_us", "telemetry/feed_ms"], 0, 1e12)
    assert len(cost["telemetry/sample_us"]["v"]) == len(got["jobs/resident"]["v"])
    assert all(v > 0 for c in cost.values() for v in c["v"])


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("core", CORES)
def test_records_account_for_every_job_call_and_submission(tmp_path, core, writer):
    run, sink, stats, submitted, lanes, snaps = run_machine(tmp_path, core, writer=writer)
    raw = run.raw(0, 1e12)
    kinds = collections.Counter(R.NAMES[int(k)] for k in raw["kind"])
    assert len(sink.records) == 60
    assert kinds["admit"] == kinds["end"] == 60
    assert kinds["enqueue"] + kinds["inline"] + kinds["hit"] == stats["calls"]
    assert kinds["result"] == kinds["enqueue"]

    # Submissions: one record each, items in aux, per lane as the lanes counted
    names = {(r["kind"], r["id"]): r["name"] for r in run.names()}
    items = collections.Counter()
    for rec in raw[raw["kind"] == R.SUBMIT]:
        items[names[("lane", int(rec["lane"]))]] += int(rec["aux"])
    assert dict(items) == {k: v for k, v in submitted.items() if v}

    # The name table holds every lane and unit
    assert {n for (k, _), n in names.items() if k == "lane"} == set(lanes)
    assert {n for (k, _), n in names.items() if k == "unit"} == {
        u for us in lanes.values() for u in us
    }

    # Calls per statement, against the counters plugin's calls per node
    prog = run.program(run.programs()[0]["start"])
    node_of = {s["addr"]: s["node"] for s in prog["statements"]}
    calls = raw[np.isin(raw["kind"], (R.ENQUEUE, R.INLINE, R.HIT))]
    by_node = collections.Counter(node_of[int(a)] for a in calls["addr"])
    counted = collections.Counter()
    for s in snaps:
        counted.update(s["work"]["calls_by_node"])
    assert by_node == counted


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("core", CORES)
def test_every_tier_agrees_with_the_raw_records(tmp_path, core, writer):
    run, *_ = run_machine(tmp_path, core, writer=writer)
    raw = run.raw(0, 1e12)
    epoch = run.info()["epoch_wall"]

    # Exact per (entity, metric) counts and sums from raw, against each tier's totals
    want = collections.Counter()
    sums = collections.Counter()
    res = raw[(raw["kind"] == R.RESULT) & ((raw["flags"] & R.SERVICE) != 0)]
    for rec in res:
        e = L.entity(L.NODE, int(rec["addr"]))
        want[(e, L.SERVICE)] += 1
        sums[(e, L.SERVICE)] += int(rec["service_us"])
    for tier in ("r1s", "r1m", "r1h"):
        rows = run.rows(tier, 0, 1e12, None, [L.SERVICE])
        got = collections.Counter()
        got_sum = collections.Counter()
        for e, n, s in zip(rows.ent.tolist(), rows.n.tolist(), rows.sum.tolist()):
            if e >> 56 == L.NODE:
                got[(e, L.SERVICE)] += n
                got_sum[(e, L.SERVICE)] += s
        assert got == want, tier
        assert got_sum == pytest.approx(sums), tier
        assert int(rows.h.sum()) == sum(n for (e, _), n in got.items() if e >> 56 == L.NODE) + int(
            rows.h[(rows.ent >> np.uint64(56)) != L.NODE].sum()
        )

    # Quantiles from the rollups are within one bin of the exact ones from raw (the value at
    # rank p * n, the rank the histogram's quantile uses)
    busiest = max(want, key=want.get)[0]
    mine = np.sort(res[res["addr"] == L.entity_id(busiest)]["service_us"].astype(float))
    h = run.hist(busiest, "service", 0, 1e12)
    assert h["n"] == len(mine)
    for p in (50, 90, 99):
        exact = mine[max(0, math.ceil(p / 100 * len(mine)) - 1)]
        b = bin_of(exact)
        assert abs(h[f"p{p}"] - exact) <= min(upper(b), mine[-1]) - lower(b), p

    # Series counts over the whole span add up to the raw count; bucket times are in range
    span = run.span()
    s = run.series(busiest, "service", span[0], span[1], 50, "count")
    assert sum(v or 0 for v in s["v"]) == want[(busiest, L.SERVICE)]
    assert all(span[0] <= t < span[1] for t in s["t"])
    assert epoch <= span[1]


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("core", CORES)
def test_traces_hold_whole_jobs(tmp_path, core, writer):
    run, *_ = run_machine(tmp_path, core, writer=writer)
    traces = run.traces()
    assert traces and {t["reason"] for t in traces} <= {"sampled", "slowest", "failed"}
    raw = run.raw(0, 1e12)
    for t in traces[:5]:
        full = run.trace(t["key"])
        kinds = collections.Counter(r["kind"] for r in full["records"])
        assert kinds["admit"] == kinds["end"] == 1
        assert len(full["records"]) == int((raw["job"] == t["job"]).sum())


@pytest.mark.parametrize("core", CORES)
def test_no_plugin_no_ring(core):
    m = build(PROGRAM, "main", n_units=1, latency_ms=0.5, core=core)
    try:
        run_jobs(m, jobs(4))
        assert m.telemetry is None
        assert all(lane.telemetry is None for lane in m.lanes)
    finally:
        m.stop()


def test_a_python_ring_on_the_native_core_is_refused(tmp_path):
    m = build(PROGRAM, "main", n_units=1, latency_ms=0.5, core="rust")
    try:
        with pytest.raises(TypeError):
            m.set_telemetry(Ring(16))
        assert m.telemetry is None
    finally:
        m.stop()


def test_a_refused_ring_leaves_no_writer_behind(tmp_path):
    before = {t.ident for t in threading.enumerate() if t.name == "togeari-telemetry"}
    t = Telemetry(tmp_path / "tel", ring="python")
    m = build(PROGRAM, "main", n_units=1, latency_ms=0.5, plugins=[t], core="rust")
    try:
        assert "telemetry" not in m.plugins.enabled and m.telemetry is None
        assert t.runner is None
        alive = {t.ident for t in threading.enumerate() if t.name == "togeari-telemetry"}
        assert alive <= before
    finally:
        m.stop()


def test_the_python_ring_is_not_read_by_a_process():
    with pytest.raises(ValueError):
        Telemetry("unused", writer="process", ring="python")


def _child_run(tmp_path, core):
    t = Telemetry(tmp_path / "tel", writer="process", flush_s=0.02, metrics_s=0.02)
    m = build(PROGRAM, "main", n_units=2, latency_ms=1.0, plugins=[t], core=core)
    return t, m


@pytest.mark.parametrize("core", CORES)
def test_the_process_writer_is_another_process_and_leaves_no_ring_file(tmp_path, core):
    t, m = _child_run(tmp_path, core)
    path = t.ring.path
    try:
        run_jobs(m, jobs(40))
        st = t.status()["writer"]
        assert st["mode"] == "process" and st["pid"] not in (None, os.getpid())
        assert st["exit"] is None
    finally:
        m.stop()
    st = t.status()["writer"]
    assert st["exit"] == 0 and st["lost"] is None and st["ticks"] > 0
    assert Run(tmp_path / "tel").info()["writer"] == "process"
    del m, t
    gc.collect()
    assert not os.path.exists(path)


def test_a_relative_directory_holds_the_process_writer_s_files(tmp_path, monkeypatch):
    # The child runs in the run directory: its files land there, not in a copy nested inside
    monkeypatch.chdir(tmp_path)
    t = Telemetry("runs/rel", writer="process", flush_s=0.02, metrics_s=0.02)
    m = build(PROGRAM, "main", n_units=1, latency_ms=0.5, plugins=[t], core="rust")
    try:
        run_jobs(m, jobs(20))
    finally:
        m.stop()
    assert t.status()["writer"]["lost"] is None
    assert (tmp_path / "runs" / "rel" / L.STATIC_FILE).is_file()
    assert not (tmp_path / "runs" / "rel" / "runs").exists()
    assert Run(tmp_path / "runs" / "rel").info()["writer"] == "process"


@pytest.mark.parametrize("core", CORES)
def test_a_writer_process_that_dies_never_stops_the_machine(tmp_path, core):
    t, m = _child_run(tmp_path, core)
    try:
        t.runner.kill()
        t.runner.reader.join(5)
        sink = run_jobs(m, jobs(40))
        assert len(sink.records) == 40 and all(r.ok for r in sink.records)
        st = t.status()
        assert st["writer"]["lost"] is not None
        assert st["ring"]["written"] > 0
    finally:
        m.stop()


def test_the_writer_process_closes_its_files_when_the_machine_s_process_is_gone(tmp_path):
    # The machine side vanishes without a stop: the child's input ends, and it closes every
    # bucket and file on its own
    t, m = _child_run(tmp_path, "rust")
    try:
        sink = run_jobs(m, jobs(40))
        assert len(sink.records) == 40
        r = t.runner
        r.halt.set()
        r.thread.join(5)
        r.proc.stdin.close()
        assert r.proc.wait(60) == 0
    finally:
        m.stop()
    run = Run(tmp_path / "tel")
    raw = run.raw(0, 1e12)
    kinds = collections.Counter(R.NAMES[int(k)] for k in raw["kind"])
    assert kinds["admit"] == kinds["end"] == 40
    rows = run.rows("r1h", 0, 1e12, None, [L.CALLS])
    assert int(rows.n.sum()) == kinds["enqueue"] + kinds["inline"] + kinds["hit"]


class _Attach(BasePlugin):
    name = "attach"

    def on_load(self, ctx):
        self.ctx = ctx


@pytest.mark.parametrize("core", CORES)
def test_a_ring_attached_mid_run_records_no_wait_from_before_it(core):
    # A statement fetched before the attach has no fetch time: its wait is absent, never the
    # time since the clock's epoch
    plug = _Attach()
    m = build(PROGRAM, "main", n_units=2, latency_ms=2.0, plugins=[plug], core=core)
    ring = plug.ctx.make_ring("auto", 1 << 16)
    at: list[int] = []

    def attach():
        at.append(ring.now_ns())
        plug.ctx.set_telemetry(ring)

    timer = threading.Timer(0.05, attach)
    try:
        timer.start()
        run_jobs(m, jobs(120))
    finally:
        timer.cancel()
        m.stop()
    recs = np.frombuffer(ring.drain(), dtype=L.REC)
    waits = recs[
        np.isin(recs["kind"], (R.ENQUEUE, R.INLINE, R.HIT)) & ((recs["flags"] & R.QUEUED) != 0)
    ]
    assert at and len(waits)
    since = (waits["t_ns"].astype(np.int64) - at[0]) / 1e3
    assert (waits["queued_us"] <= since).all()


@pytest.mark.parametrize("core", CORES)
def test_a_refused_write_is_retried_and_no_tier_loses_rows(tmp_path, core, monkeypatch):
    # The first write to the raw and to the 1 s files raises; a later tick (or the tries at
    # stop) writes them
    refusals = collections.Counter()
    real = Segments.append

    def flaky(self, t, data):
        if self.name in ("raw", "r1s") and not refusals[self.name]:
            refusals[self.name] += 1
            raise OSError("disk full")
        return real(self, t, data)

    monkeypatch.setattr(Segments, "append", flaky)
    run, sink, stats, *_ = run_machine(tmp_path, core, flush_s=0.02, writer="thread", clean=False)
    raw = run.raw(0, 1e12)
    kinds = collections.Counter(R.NAMES[int(k)] for k in raw["kind"])
    assert refusals == {"raw": 1, "r1s": 1}
    assert kinds["admit"] == kinds["end"] == 60
    assert kinds["enqueue"] + kinds["inline"] + kinds["hit"] == stats["calls"]
    for tier in ("r1s", "r1m", "r1h"):
        rows = run.rows(tier, 0, 1e12, None, [L.CALLS])
        assert int(rows.n.sum()) == stats["calls"], tier


def test_a_stream_opened_after_disable_does_not_subscribe():
    insp = Inspector(token=None)
    m = build(PROGRAM, "main", n_units=1, latency_ms=0.5, plugins=[insp], core="python")
    try:
        assert m.plugins.disable(insp.name)
        insp.open_stream(None)
        assert id(insp) not in m.bus.subs
    finally:
        m.stop()


def test_store_off_drains_and_writes_nothing(tmp_path):
    run, sink, *_ = run_machine(tmp_path, "python", n=10, store=False)
    assert len(sink.records) == 10
    assert len(run.raw(0, 1e12)) == 0 and run.span() is None


@pytest.mark.parametrize("core", CORES)
def test_the_inspector_subscribes_only_while_a_stream_is_open(core):
    insp = Inspector(token=None)
    m = build(PROGRAM, "main", n_units=1, latency_ms=0.5, plugins=[insp], core=core)
    try:
        bus = m.bus
        assert id(insp) not in bus.subs and not bus.wants("stmt.fetch", None)
        q = insp.open_stream({"job.end"})
        assert bus.subs[id(insp)].level == "counters" and not bus.wants("stmt.fetch", None)
        q2 = insp.open_stream(None)
        assert bus.subs[id(insp)].level == "all" and bus.wants("stmt.fetch", None)
        insp.close_stream(q2)
        assert bus.subs[id(insp)].level == "counters"
        insp.close_stream(q)
        assert id(insp) not in bus.subs
        run_jobs(m, jobs(3))
    finally:
        m.stop()
