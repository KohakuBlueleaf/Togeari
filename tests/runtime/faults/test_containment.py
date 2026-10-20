"""Containment: user policies that raise or misbehave, a plugin disabled while its event thread
waits on the dispatcher, and every caller answered when the dispatcher dies."""

import logging
import threading
import time

import pytest
from kohakunode import NodeLibrary
from rt_helpers import ENTERED, RELEASE, in_thread, machine, sim_lane, wait_for

from togeari import BasePlugin, DispatcherDied, Inline, Lane, Machine, Use
from togeari.core.contracts.io import JobRecord
from togeari.core.contracts.results import Cancelled, UnitError
from togeari.core.runtime.handles import JobHandle, Reply
from togeari.io import Iterable, Memory
from togeari.policies import Fixed, LeastLoaded, OldestJobFirst, SizeOrWait
from togeari.testing import SimBatch, SimCall

logging.disable(logging.CRITICAL)

LIB = NodeLibrary("contain")
CALLED: list[int] = []
NOTED: list[int] = []


@LIB.node()
def rec(x: int) -> int:
    CALLED.append(x)
    return x


@LIB.node()
def step(x: int) -> int:
    return x + 1


@LIB.node(ordered=True)
def note(x: int) -> int:
    NOTED.append(x)
    return x


FOUR = """
@def (a: int, b: int, c: int, d: int)main(y):
    (a)rec(p)
    (b)rec(q)
    (c)rec(r)
    (d)rec(s)
    y = p
"""

CHAIN_THEN_NOTE = """
@def (x: int)main(y):
    (x)step(a)
    (a)step(b)
    (b)step(c)
    (c)step(d)
    (x)note(n)
    y = d
"""

STEP = "@def (x: int)main(y):\n    (x)step(y)\n"


def contained(program: str, bind: dict, **kw) -> Machine:
    bind.setdefault("kir.std.*", Inline())
    return Machine(program=program, entry="main", libraries=[LIB], bind=bind, **kw)


def step_lane(**kw) -> Lane:
    return Lane([SimCall(name="steps", latency_ms=20, tail=0.0, capacity=8)], **kw)


# ---------------------------------------------------------------- issue order
class Picks(OldestJobFirst):
    def order(self, ready):
        if len(ready) != 4:
            return ready
        by_key = sorted(ready, key=lambda v: v.key)
        return [by_key[3], by_key[1]]


def test_left_out_statements_issue_after_the_chosen_ones_in_arrival_order():
    CALLED.clear()
    m = contained(FOUR, {"rec": Inline()}, issue=Picks())
    try:
        assert m.submit({"a": 1, "b": 2, "c": 3, "d": 4}).result(5).ok
    finally:
        m.stop()
    assert CALLED == [4, 2, 1, 3]


class Doubles(OldestJobFirst):
    def __init__(self):
        self.notes_seen: list[int] = []

    def order(self, ready):
        self.notes_seen.append(sum(1 for v in ready if v.node == "note"))
        return [*ready, *ready]


def test_an_issue_order_with_duplicates_issues_each_statement_once():
    NOTED.clear()
    policy = Doubles()
    m = contained(CHAIN_THEN_NOTE, {"step": Use(step_lane()), "note": Inline()}, issue=policy)
    try:
        rec = m.submit({"x": 7}).result(5)
    finally:
        m.stop()
    assert rec.ok and rec.outputs == {"y": 11}
    assert NOTED == [7]
    assert max(policy.notes_seen) == 1


class Foreign(OldestJobFirst):
    def order(self, ready):
        return [object(), "view", None, *reversed(ready)]


def test_objects_that_are_not_views_of_the_batch_are_skipped():
    m = contained(FOUR, {"rec": Inline()}, issue=Foreign())
    try:
        recs = [m.submit({"a": i, "b": 2, "c": 3, "d": 4}).result(5) for i in range(5)]
    finally:
        m.stop()
    assert all(r.ok and r.outputs == {"y": i} for i, r in enumerate(recs))
    assert m.dispatcher.stats.get("policy_errors", 0) == 0


class Raises(OldestJobFirst):
    def order(self, ready):
        raise RuntimeError("order bug")


def test_a_raising_issue_order_issues_in_arrival_order_and_jobs_finish():
    CALLED.clear()
    m = contained(FOUR, {"rec": Inline()}, issue=Raises())
    try:
        recs = [m.submit({"a": 1, "b": 2, "c": 3, "d": 4}, key=str(i)).result(5) for i in range(3)]
        assert m.dispatcher.dead is None
    finally:
        m.stop()
    assert all(r.ok for r in recs)
    assert CALLED == [1, 2, 3, 4] * 3
    assert m.dispatcher.stats["policy_errors"] >= 3
    assert m.dispatcher.stats["machine_errors"] >= 3


# ---------------------------------------------------------------- admission
class RaisingAdmission(Fixed):
    def allows(self, pool, signals):
        raise RuntimeError("admission bug")


def test_a_raising_admission_rule_admits_one_job_at_a_time_and_the_run_ends():
    m = contained(STEP, {"step": Use(step_lane())}, admission=RaisingAdmission(8))
    sink = Memory()
    peak = []
    m.bus.listen(lambda kind, fields: peak.append(len(m.dispatcher.jobs)), kinds=["job.admit"])
    try:
        finished, got = in_thread(lambda: m.run(Iterable([{"x": i} for i in range(10)]), sink), 10)
    finally:
        m.stop()
    assert finished and not isinstance(got, BaseException), got
    assert sorted(r.outputs["y"] for r in sink.records) == list(range(1, 11))
    assert max(peak) == 1
    assert m.dispatcher.stats["policy_errors"] >= 9


# ---------------------------------------------------------------- lane policies
class OrderRaises(LeastLoaded):
    def order(self, units, lane):
        raise RuntimeError("selector order bug")


class AdmitsRaises(LeastLoaded):
    def admits(self, item, unit, lane):
        raise RuntimeError("selector admits bug")


class TakeRaises(SizeOrWait):
    def take(self, bucket, unit, now):
        raise RuntimeError("former take bug")


class KeyRaises(SizeOrWait):
    def key(self, item, unit):
        raise RuntimeError("former key bug")


class DueRaises(SizeOrWait):
    def due(self, item):
        raise RuntimeError("former due bug")


class DeadlineRaises(SizeOrWait):
    def deadline(self, bucket, now):
        raise RuntimeError("former deadline bug")


def batch_lane(former) -> Lane:
    return Lane([SimBatch(name="b", latency_ms=5, tail=0.0, max_batch=4)], former=former)


@pytest.mark.parametrize(
    "lane",
    [
        lambda: step_lane(select=OrderRaises()),
        lambda: step_lane(select=AdmitsRaises()),
        lambda: step_lane(select=AdmitsRaises(), forming="early"),
        lambda: batch_lane(TakeRaises(4, 1)),
        lambda: batch_lane(KeyRaises(4, 1)),
        lambda: batch_lane(DueRaises(4, 1)),
        lambda: batch_lane(DeadlineRaises(4, 1)),
    ],
    ids=["order", "admits", "admits-early", "take", "key", "due", "deadline"],
)
def test_a_raising_lane_policy_falls_back_and_every_job_finishes(lane):
    lane = lane()
    m = contained(STEP, {"step": Use(lane)})
    try:
        recs = [h.result(5) for h in [m.submit({"x": i}) for i in range(12)]]
        assert m.dispatcher.dead is None
    finally:
        m.stop()
    assert [r.outputs for r in recs] == [{"y": i + 1} for i in range(12)]
    assert lane.stats["policy_errors"] >= 1


# ---------------------------------------------------------------- post_result results
@pytest.mark.parametrize("kind", ["UnitError", "Cancelled"])
def test_post_result_may_replace_a_result_with_any_result_type(kind):
    replacement = UnitError(RuntimeError("replaced")) if kind == "UnitError" else Cancelled()

    class Replace(BasePlugin):
        name = "replace"

        def post_result(self, result, **ctx):
            return replacement

    m = machine(plugins=[Replace()])
    try:
        rec = m.submit({"x": 1}).result(5)
        assert "replace" in m.plugins.enabled
    finally:
        m.stop()
    assert not rec.ok and rec.failure["node"] == "inc"


# ---------------------------------------------------------------- disabling a plugin
class WaitsOnDispatcher(BasePlugin):
    name = "waits"
    events = "all"
    close_timeout_s = 2.0

    def __init__(self):
        self.go = threading.Event()
        self.answered = threading.Event()

    def on_load(self, ctx):
        self.ctx = ctx

    def on_event(self, ev):
        if ev.kind == "job.admit":
            self.go.wait(5)
            self.ctx.inspect("jobs").result(3)
            self.answered.set()

    def on_job_end(self, record):
        self.go.set()
        time.sleep(0.2)
        raise RuntimeError("plugin bug")


def test_disabling_a_plugin_whose_event_thread_waits_on_the_dispatcher_does_not_block():
    p = WaitsOnDispatcher()
    m = machine(plugins=[p])
    try:
        t0 = time.monotonic()
        rec = m.submit({"x": 1}).result(10)
        dt = time.monotonic() - t0
        assert rec.ok
        assert "waits" not in m.plugins.enabled
        assert dt < 1.5, dt
        assert p.answered.wait(3)
    finally:
        m.stop()


# ---------------------------------------------------------------- dispatcher death
BLOCK_THEN_INC = "@def (x: int)main(y):\n    (x)block(a)\n    (a)inc(y)\n"


def _broken(now):
    raise RuntimeError("lane bug")


def test_queries_commands_stop_and_submits_queued_before_death_are_answered():
    ENTERED.clear()
    RELEASE.clear()
    lane = sim_lane()
    m = machine(program=BLOCK_THEN_INC, bind={"block": Inline(), "inc": Use(lane)})
    try:
        first = m.submit({"x": 1})
        assert ENTERED.wait(5)

        # Posted while the dispatcher is inside the inline call; the next pump raises
        query = m.inspect("stats")
        command = m.command("set_knob", name="window", value=32)
        stop_reply = Reply()
        m.dispatcher.inbox.put(("stop", True, stop_reply))
        later = m.submit({"x": 2})
        lane.pump = _broken
        RELEASE.set()

        for reply in (query, command, stop_reply):
            with pytest.raises(DispatcherDied):
                reply.result(5)
        assert first.result(5).failure["kind"] == "DispatcherDied"
        assert later.result(5).failure["kind"] == "DispatcherDied"
    finally:
        RELEASE.set()
        m.stop()


def test_submitters_racing_a_death_all_get_an_answer():
    lane = sim_lane(latency_ms=0.5)
    m = machine(bind={"inc": Use(lane)})
    handles: list = []
    refused = [0]
    lock = threading.Lock()
    start = threading.Event()

    def submitter(n):
        start.wait(5)
        for i in range(200):
            try:
                h = m.submit({"x": i}, key=f"{n}-{i}")
            except DispatcherDied:
                with lock:
                    refused[0] += 1
                continue
            with lock:
                handles.append(h)

    threads = [threading.Thread(target=submitter, args=(n,), daemon=True) for n in range(4)]
    try:
        for t in threads:
            t.start()
        start.set()
        assert wait_for(lambda: len(handles) >= 50)
        lane.pump = _broken
        for t in threads:
            t.join(10)
        assert not any(t.is_alive() for t in threads)
        recs = [h.result(5) for h in handles]
    finally:
        m.stop()
    assert m.dispatcher.dead is not None
    assert len(recs) + refused[0] == 800
    kinds = {r.failure["kind"] for r in recs if not r.ok}
    assert kinds <= {"DispatcherDied"} and "DispatcherDied" in kinds


def test_a_handle_and_a_reply_keep_their_first_answer():
    h = JobHandle("k")
    first = JobRecord("k", "main", False, {}, {"kind": "DispatcherDied"})
    h._set(first)
    h._set(JobRecord("k", "main", True, {"y": 1}, None))
    assert h.result(0) is first

    r = Reply()
    r._set(error=DispatcherDied("dead"))
    r._set(value=1)
    with pytest.raises(DispatcherDied):
        r.result(0)
