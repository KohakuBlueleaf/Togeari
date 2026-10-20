"""What policies and plugins see: read-only views, per-kind listeners, levels, query reprs."""

import queue

import pytest
from rt_helpers import machine, sim_lane

from togeari import BasePlugin, Use
from togeari.core.contracts.views import PoolView, ReadyView
from togeari.core.observe.bus import CORE_EVENTS, Bus
from togeari.core.policies.admission import Fixed
from togeari.core.policies.issue import OldestJobFirst
from togeari.io import Iterable, Memory


# ---------------------------------------------------------------- policy views
class SeenIssue(OldestJobFirst):
    def __init__(self):
        self.seen = []
        self.fields = []

    def order(self, ready):
        self.seen += [type(r) for r in ready]
        return super().order(ready)

    def priority(self, st):
        self.seen.append(type(st))
        self.fields.append((st.job_key, st.entry, st.node, st.lane))
        return super().priority(st)


class SeenAdmission(Fixed):
    def __init__(self):
        super().__init__(4)
        self.seen = []

    def allows(self, pool, signals):
        self.seen.append(type(pool))
        return super().allows(pool, signals)


def test_policies_get_read_only_views():
    # Issue policies see ReadyViews only, admission rules PoolViews only
    issue, admission = SeenIssue(), SeenAdmission()
    m = machine(issue=issue, admission=admission)
    try:
        assert m.submit({"x": 1}, key="only").result(5).ok
        m.run(Iterable([{"x": i} for i in range(6)]), Memory())
    finally:
        m.stop()
    assert issue.seen and set(issue.seen) == {ReadyView}
    assert issue.fields[0] == ("only", "main", "inc", "s-lane")
    assert admission.seen and set(admission.seen) == {PoolView}


class ReturnsNothing(OldestJobFirst):
    def order(self, ready):
        return []


def test_statements_an_issue_policy_leaves_out_still_issue():
    # Statements the policy leaves out are issued after the ones it returns: every job ends
    m = machine(issue=ReturnsNothing())
    try:
        handles = [m.submit({"x": i}, key=str(i)) for i in range(5)]
        assert all(h.result(5).ok for h in handles)
    finally:
        m.stop()


def test_a_ready_view_cannot_be_written():
    view = ReadyView(None)
    with pytest.raises(AttributeError):
        view.key = (1,)


# ---------------------------------------------------------------- per-kind listeners
def test_a_per_kind_listener_builds_only_its_kind():
    # A listener for one kind makes only that kind's detail events get built
    m = machine(events="sampled", event_sample=0.0)
    got = []
    m.bus.listen(lambda kind, fields: got.append(kind), kinds=["stmt.fetch"])
    try:
        m.run(Iterable([{"x": i} for i in range(5)]), Memory())
    finally:
        m.stop()
    assert got == ["stmt.fetch"] * 5
    detail = set(m.bus.counts) - CORE_EVENTS
    assert detail == {"stmt.fetch"}, detail


def test_wants_follows_listeners_and_levels():
    bus = Bus({"events": "sampled"})
    assert not bus.wants("stmt.ready", 1) and not bus.wants("job.end", 1)
    fn = lambda kind, fields: None  # noqa: E731
    bus.listen(fn, kinds=("stmt.fetch",))
    assert bus.wants("stmt.fetch", 1) and not bus.wants("stmt.ready", 1)
    bus.unlisten(fn)
    assert not bus.wants("stmt.fetch", 1) and not bus.active


# ---------------------------------------------------------------- core events at every level
class CountersSub(BasePlugin):
    name = "counters_sub"
    events = "counters"

    def __init__(self):
        self.q = queue.Queue()

    def on_event(self, ev):
        self.q.put(ev.kind)


def test_a_counters_level_subscriber_receives_core_events():
    # A "counters" subscriber receives the core events too, and no detail events
    bus = Bus({"events": "all"})
    p = CountersSub()
    bus.subscribe(p)
    bus.emit("job.end", job=1)
    bus.emit("stmt.fetch", job=1)
    bus.emit("counters", source="x")
    bus.close()
    assert [p.q.get() for _ in range(p.q.qsize())] == ["job.end", "counters"]


# ---------------------------------------------------------------- bounded reprs
class Loud:
    reprs = 0

    def __repr__(self):
        Loud.reprs += 1
        return "loud"


def test_the_job_query_reprs_inputs_within_bounds():
    # The job query reprs a few elements of a large input, within 200 characters
    lane = sim_lane(latency_ms=2000, tail=0.0)
    m = machine(program="@def (xs: list)main(y):\n    (xs)ident(y)\n", bind={"ident": Use(lane)})
    try:
        m.submit({"xs": [Loud() for _ in range(1000)]})
        jobs = m.inspect("jobs").result()
        Loud.reprs = 0
        info = m.inspect("job", id=jobs[0]["id"]).result()
        assert Loud.reprs <= 10 and len(info["inputs"]["xs"]) <= 200
    finally:
        m.stop(drain=False, timeout=5)
