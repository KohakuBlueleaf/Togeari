"""Job and unit lifecycle through the machine: cancel, detach with drain, a dead lane, a hung
worker past the lane timeout, snapshot clean-up after a resume, plugin entry gating."""

import logging
import time

from feature_nodes import LIB

from conftest import CountOutage, bounded
from togeari import BasePlugin, Inline, Lane, Machine, Retry, Use
from togeari.engines.python import PythonCallEngine
from togeari.stores import MemoryStore
from togeari.testing import SimCall

logging.disable(logging.CRITICAL)

DOUBLE = "@def (x: int)main(y):\n    (x)double(a)\n    (a)double(y)\n"
NAP = "@def (x: int, ms: int)main(y):\n    (x, ms)nap(y)\n"
LOOP = """
@def (n: int)main(x):
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


def machine(program: str, bind: dict, **kw) -> Machine:
    bind.setdefault("kir.std.*", Inline())
    return Machine(program=program, entry="main", libraries=[LIB], bind=bind, **kw)


def until(m, cond, seconds: float = 10.0) -> None:
    """Wait for ``cond()``; past ``seconds`` the check fails with the machine's dump."""

    def wait():
        while not cond():
            time.sleep(0.005)

    bounded(m, wait, seconds)


def job_id(m, key: str) -> int:
    return next(j["id"] for j in m.inspect("jobs").result() if j["key"] == key)


def test_cancel_job_answers_its_handle_and_leaves_the_others():
    m = machine(DOUBLE, {"double": Use(Lane([SimCall(name="c", latency_ms=200)]))})
    try:
        hs = {k: m.submit({"x": i}, key=k) for i, k in enumerate("abc")}
        until(m, lambda: len(m.inspect("jobs").result()) == 3)
        m.command("cancel_job", id=job_id(m, "b")).result()
        recs = {k: bounded(m, lambda h=h: h.result(10), 15) for k, h in hs.items()}
        assert recs["b"].failure["kind"] == "Cancelled"
        assert recs["a"].outputs == {"y": 0} and recs["c"].outputs == {"y": 8}
    finally:
        m.stop(drain=False, timeout=5)


def test_detach_with_drain_finishes_the_units_work_and_detaches_it():
    a = SimCall(name="a", latency_ms=20, capacity=4)
    b = SimCall(name="b", latency_ms=20, capacity=4)
    m = machine(DOUBLE, {"double": Use(Lane([a, b], name="work"))})
    try:
        hs = [m.submit({"x": i}) for i in range(30)]
        until(m, lambda: a.load().running > 0)
        m.detach("a", drain=True)
        recs = [bounded(m, lambda h=h: h.result(20), 25) for h in hs]
        assert [r.outputs for r in recs] == [{"y": 4 * i} for i in range(30)]

        # Detached once its last item settled; no work reached it after the detach
        until(
            m,
            lambda: {u["name"]: u for u in m.inspect("units").result()}["a"]["state"] == "detached",
        )
        done_a = a.counts["items"]
        m.submit({"x": 1}).result(10)
        assert a.counts["items"] == done_a
    finally:
        m.stop(drain=False, timeout=5)


def test_a_dead_lane_fails_the_job_instead_of_holding_it():
    u = CountOutage("call", name="only", latency_ms=1)
    lane = Lane([u], lane_dead_s=0.2, retry=Retry(max=10, backoff_ms=50))
    m = machine(DOUBLE, {"double": Use(lane)})
    try:
        u.go_down(polls=10**9)
        rec = bounded(m, lambda: m.submit({"x": 1}).result(10), 15)
        assert not rec.ok and rec.failure["kind"] == "UnitUnavailable", rec.failure
        assert lane.stats["dead_failed"] >= 1
    finally:
        m.stop(drain=False, timeout=5)


def test_a_hung_worker_is_killed_at_the_lane_timeout_and_the_next_job_runs():
    engine = PythonCallEngine(workers=1, mode="process")
    m = machine(NAP, {"nap": Use(Lane([engine], timeout_s=1.0))}, start_timeout_s=60)
    try:
        hung = bounded(m, lambda: m.submit({"x": 1, "ms": 600_000}).result(60), 65)
        assert hung.failure["kind"] == "ItemTimeout", hung.failure

        # The worker was killed and replaced: the slot serves the next job
        rec = bounded(m, lambda: m.submit({"x": 2, "ms": 1}).result(60), 65)
        assert rec.ok and rec.outputs == {"y": 2}
        assert engine.load().extra["kills"] == 1
    finally:
        m.stop(drain=False, timeout=5)


def test_a_resumed_job_that_succeeds_leaves_no_snapshot():
    store = MemoryStore()
    lane = Lane([SimCall(name="s", latency_ms=20)])

    # First run: stopped at its first snapshot (the next one is 8 calls later)
    m = machine(LOOP, {"step": Use(lane)}, store=store, snapshot_every=8, run_ahead=False)
    try:
        h = m.submit({"n": 40}, key="k")
        until(m, lambda: len(store) > 0)
    finally:
        m.stop(drain=False, timeout=5)
    assert h.result(5).failure["kind"] == "Cancelled"

    # Second run: resumes, takes no new snapshot, succeeds; the old snapshot is gone
    lane = Lane([SimCall(name="s", latency_ms=1)])
    m = machine(LOOP, {"step": Use(lane)}, store=store, snapshot_every=10**6)
    try:
        rec = bounded(m, lambda: m.submit({"n": 40}, key="k").result(10), 15)
    finally:
        m.stop(drain=False, timeout=5)
    assert rec.ok and rec.outputs == {"x": 40} and rec.stats.get("resumed")
    assert len(store) == 0


class Ends(BasePlugin):
    """Keys of the jobs whose end it saw; gated to ``entries`` when given."""

    def __init__(self, name: str, entries: list | None = None):
        self.name = name
        self.applies_to = {"entries": entries} if entries is not None else None
        self.keys = []

    def on_job_end(self, record):
        self.keys.append(record.key)


def test_applies_to_entries_gates_hooks_by_entry():
    other = "@def (x: int)other(y):\n    (x)double(y)\n"
    gated, free = Ends("gated", ["other"]), Ends("free")
    m = Machine(
        programs={DOUBLE: ["main"], other: ["other"]},
        libraries=[LIB],
        bind={"double": Use(Lane([SimCall(name="g", latency_ms=1)]))},
        plugins=[gated, free],
    )
    try:
        for i in range(3):
            assert m.submit({"x": i}, key=f"m{i}", entry="main").result(10).ok
            assert m.submit({"x": i}, key=f"o{i}", entry="other").result(10).ok
    finally:
        m.stop(drain=False, timeout=5)
    assert sorted(gated.keys) == ["o0", "o1", "o2"]
    assert len(free.keys) == 6
