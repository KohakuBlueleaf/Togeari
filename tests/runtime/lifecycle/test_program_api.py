"""Construction, loading, units, `each`, events and the public API around them."""

import logging
import queue
import threading
import time

import pytest
from rt_helpers import INC, LIB, SLOW, machine, plugin, sim_lane

from togeari import BasePlugin, ConstructionError, Inline, Lane, Machine, Use, no_unit_start
from togeari.core.construct.errors import Problems
from togeari.core.contracts.adapter import PythonAdapter
from togeari.core.observe.bus import Bus
from togeari.core.program.code import CodeMemory
from togeari.core.program.job import build_job_programs
from togeari.io import Iterable, Memory
from togeari.policies import Fifo
from togeari.stores import MemoryStore
from togeari.testing import SimCall
from togeari.testing.sim import SimEngine

WIDE = """
@def (n: int)main(total):
    (n)range(items)
    total = 0
    (items)each(i, `one`)
    one:
        (i)inc(j)
        (total, j)add(total)
"""


def test_each_starts_iterations_as_the_window_allows():
    m = machine(program=WIDE, bind={"inc": Use(sim_lane(latency_ms=0.2, capacity=64))}, window=16)
    try:
        rec = m.submit({"n": 1000}).result(20)
        assert rec.outputs == {"total": sum(range(1, 1001))}
        assert rec.stats["strands_peak"] < 100
    finally:
        m.stop()


REPORT = """
@def (x: int, xs: list)main(summary, scores):
    ()parallel(`a`, `b`)
    a:
        (x)inc(sky)
    b:
        (x)inc(n_events)
    (x)inc(jam)
    (sky)isbig(storm)
    (storm)branch(`warn`, `calm`)
    warn:
        (x)inc(note)
    calm:
        note = 0
    scores = []
    (xs)each(d, `one`)
    one:
        (d)inc(score)
        (scores, score)append(scores)
    (sky, jam)add(summary)
"""


def test_strands_and_inflight_peaks_count_everything_at_once():
    lane = sim_lane(latency_ms=50, capacity=64)
    m = machine(program=REPORT, bind={"inc": Use(lane), "isbig": Use(lane)})
    try:
        rec = m.submit({"x": 3, "xs": [1, 2, 3, 4, 5]}).result(10)
        assert rec.ok and rec.outputs["scores"] == [2, 3, 4, 5, 6]
        assert rec.stats["strands_peak"] >= 3
        assert rec.stats["inflight_peak"] >= 5
    finally:
        m.stop()


def test_carried_names_are_those_live_at_the_body_entry():
    prog = """
@def (xs: list)main(acc, last):
    acc = 0
    last = 0
    (xs)each(i, `body`)
    body:
        (i)inc(t)
        (acc, t)add(acc)
        last = t
"""
    jobs = build_job_programs(prog, ["main"], [LIB], None, None, Problems())
    code = CodeMemory()
    region = code.load(jobs[0])
    each = next(a for a in range(region.start, region.end) if code.mem[a].op == "each")
    t = region.tables
    assert t.body_may_write(each) == {"t", "acc", "last"}
    assert t.carried(each) == {"acc"}


def test_unit_names_unique_and_one_unit_in_two_lanes():
    a, b = SimCall(name="same"), SimCall(name="same")
    with pytest.raises(ConstructionError, match="used twice"):
        machine(bind={"inc": Use(Lane([a, b]))})
    u = SimCall(name="shared")
    prog = "@def (x: int)main(y):\n    (x)inc(a)\n    (a)ident(y)\n"
    m = machine(
        program=prog, bind={"inc": Use(Lane([u], name="l1")), "ident": Use(Lane([u], name="l2"))}
    )
    m.stop()


class Named(BasePlugin):
    name = "named"

    def contribute_policies(self):
        return {"plain": Fifo()}


def test_policies_contributed_by_name():
    m = machine(issue="plain", plugins=[Named()])
    try:
        assert type(m.knobs["issue"]) is Fifo
        assert m.submit({"x": 1}).result(5).ok
    finally:
        m.stop()
    with pytest.raises(ConstructionError, match="no contributed policy"):
        machine(issue="missing")


class Starts(SimEngine):
    started = 0

    def start(self):
        Starts.started += 1
        super().start()


def test_no_unit_start_builds_without_starting_and_cannot_run():
    Starts.started = 0
    with no_unit_start():
        m = machine(bind={"inc": Use(Starts(name="st"))})
    assert Starts.started == 0
    with pytest.raises(RuntimeError, match="without starting"):
        m.submit({"x": 1})
    m.stop()


def test_a_str_program_is_text_and_a_path_is_a_file(tmp_path):
    path = tmp_path / "p.kir"
    path.write_text(INC, encoding="utf-8")
    with pytest.raises(ConstructionError):
        machine(program=str(path))
    machine(program=path).stop()


class Tagged:
    protocols = {"tagged"}
    light = True

    def __init__(self, fn=None):
        self.fn = fn

    def bound(self, fn):
        return Tagged(fn)

    def check(self, signature):
        return []

    def prepare(self, inputs, protocol):
        return PythonAdapter(self.fn).prepare(inputs, protocol)

    def decode(self, raw, protocol):
        return raw


def test_the_default_adapter_and_protocol_are_machine_arguments():
    m = machine(adapter=Tagged(), protocol="tagged")
    try:
        assert m.submit({"x": 1}).result(5).outputs == {"y": 2}
        assert isinstance(m.code.mem[m.code.entries["main"].addr].binding.adapter, Tagged)
    finally:
        m.stop()


def test_a_failed_load_changes_nothing():
    m = machine()
    try:
        with pytest.raises(ConstructionError):
            m.load("@def (x: int)two(y):\n    (x)unbound(y)\n", ["two"])
        assert list(m.code.entries) == ["main"]
        assert m.submit({"x": 1}, entry="two").result(5).failure["kind"] == "KirRuntimeError"
        assert m.submit({"x": 1}).result(5).ok
    finally:
        m.stop()


def test_a_load_with_a_new_unit_starts_it_before_its_entry_exists():
    u = SimCall(name="late")
    m = machine(bind={"inc": Use(sim_lane()), "ident": Use(u)})
    try:
        # Predeclared but unused bindings still start lazily, not at construction.
        assert not u.ready() and "two" not in m.code.entries
        names = m.load("@def (x: int)two(y):\n    (x)ident(y)\n", ["two"])
        assert names == ["two"] and u.ready()
        assert m.submit({"x": 3}, entry="two").result(5).outputs == {"y": 3}
    finally:
        m.stop()


def test_attach_checks_the_unit():
    lane = sim_lane()
    m = machine(bind={"inc": Use(lane)})
    try:
        with pytest.raises(ConstructionError, match="already attached"):
            m.attach(lane, lane.units[0])
        with pytest.raises(ConstructionError, match="used twice"):
            m.attach(lane, SimCall(name=lane.units[0].name))
        assert m.attach(lane, SimCall(name="s2")) == "s2"
        assert m.submit({"x": 1}).result(5).ok
    finally:
        m.stop()


def test_content_keys_survive_attaching_another_unit_of_the_same_engine():
    lane = sim_lane()
    m = machine(bind={"inc": Use(lane, store="content")}, store=MemoryStore())
    try:
        m.submit({"x": 1}).result(5)
        m.attach(lane, SimCall(name="twin"))
        m.submit({"x": 1}).result(5)
        assert m.inspect("stats").result()["store_hits"] == 1
    finally:
        m.stop()


class Sink(BasePlugin):
    name = "sink"
    events = "all"

    def __init__(self):
        self.q = queue.Queue()

    def on_event(self, ev):
        self.q.put(ev)


def test_events_cover_ready_strands_and_hooks_with_addresses():
    s = Sink()
    prog = (
        "@def (x: int)main(y):\n    (x)isbig(c)\n    (c)branch(`yes`, `no`)\n    yes:\n"
        "        (x)inc(y)\n    no:\n        y = 0\n"
    )
    lane = sim_lane(latency_ms=20)
    p = plugin("touch", on_job_end=lambda rec: None)
    m = machine(
        program=prog, bind={"inc": Use(lane), "isbig": Use(lane)}, plugins=[s, p], events="all"
    )
    try:
        m.submit({"x": 9}).result(5)
    finally:
        m.stop()
    kinds = {}
    while not s.q.empty():
        ev = s.q.get()
        kinds.setdefault(ev.kind, ev.fields)
    for k in ("stmt.ready", "strand.park", "strand.resume", "strand.end", "plugin.hook"):
        assert k in kinds, k
    assert "addr" in kinds["stmt.issue"] and kinds["stmt.issue"]["lane"] == lane.name


def test_counters_level_listeners_get_the_job_field():
    knobs = {"events": "counters"}
    bus = Bus(knobs)
    got = []
    bus.listeners.append(lambda kind, fields: got.append(dict(fields)))
    bus.emit("stmt.fetch", job=7, node="n")
    assert got == [{"node": "n", "job": 7}]


class Slow(BasePlugin):
    name = "slow"
    events = "all"
    overflow = "counters_only"
    queue_size = 4

    def __init__(self):
        self.go = threading.Event()

    def on_event(self, ev):
        self.go.wait(5)


def test_a_full_subscriber_queue_lowers_its_level_until_it_drains():
    bus = Bus({"events": "all"})
    p = Slow()
    bus.subscribe(p)
    sub = bus.subs[id(p)]
    for i in range(10):
        bus.emit("stmt.fetch", job=None, i=i)
    assert sub.current == "counters"
    p.go.set()
    deadline = time.monotonic() + 5
    while sub.q.qsize() and time.monotonic() < deadline:
        time.sleep(0.01)
    bus.emit("stmt.fetch", job=None)
    assert sub.current == "all"
    bus.close()


class SampledSub(BasePlugin):
    name = "sampled_sub"
    events = "sampled"

    def __init__(self):
        self.q = queue.Queue()

    def on_event(self, ev):
        self.q.put(ev)


def test_sampled_subscribers_get_sampled_jobs_only_and_sampling_follows_record():
    bus = Bus({"events": "all"})
    p = SampledSub()
    bus.subscribe(p)
    bus.sample(1, True)
    bus.sample(2, False)
    bus.emit("stmt.fetch", job=1)
    bus.emit("stmt.fetch", job=2)
    bus.close()
    jobs = [p.q.get().fields["job"] for _ in range(p.q.qsize())]
    assert jobs == [1]

    s = SampledSub()
    m = machine(plugins=[s], record=1.0)
    try:
        m.run(Iterable([{"x": i} for i in range(20)]), Memory())
    finally:
        m.stop()
    fetched = set()
    while not s.q.empty():
        ev = s.q.get()
        if ev.kind == "stmt.fetch":
            fetched.add(ev.fields["job"])
    assert len(fetched) == 20


def test_a_slow_inline_node_is_reported_after_many_fast_runs(caplog):
    prog = "@def (x: int)main(y):\n    (x)slow_inline(y)\n"
    m = machine(program=prog, bind={"slow_inline": Inline(warn_us=100)})
    try:
        with caplog.at_level(logging.WARNING, logger="togeari.dispatcher"):
            SLOW[0] = False
            m.run(Iterable([{"x": i} for i in range(300)]), Memory())
            assert not any("slow_inline" in r.message for r in caplog.records)
            SLOW[0] = True
            m.run(Iterable([{"x": i} for i in range(800)]), Memory())
        assert sum("slow_inline" in r.message for r in caplog.records) == 1
    finally:
        SLOW[0] = False
        m.stop()


def test_region_lookup_over_several_programs():
    m = Machine(
        programs={INC: ["main"], "@def (x: int)two(y):\n    (x)ident(y)\n": ["two"]},
        libraries=[LIB],
        bind={"inc": Inline(), "ident": Inline()},
    )
    try:
        for name, e in m.code.entries.items():
            assert m.code.region_of(e.addr) is e.region, name
        assert m.submit({"x": 1}, entry="two").result(5).outputs == {"y": 1}
    finally:
        m.stop()
