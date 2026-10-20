"""Admission sees the load each admitted job adds; stop() delivers every queued event."""

import time

from kohakunode import NodeLibrary

from togeari import BasePlugin, Inline, Lane, Machine, Use
from togeari.io import Iterable, Memory
from togeari.policies import TargetLoad
from togeari.testing import SimCall

lib = NodeLibrary("admission")


@lib.node(outputs=["y"])
def work(x: int) -> int:
    return x + 1


PROGRAM = "@def (x: int)main(y):\n    (x)work(y)\n"


class Peak(BasePlugin):
    """The largest number of resident jobs seen at a job start."""

    name = "peak"

    def __init__(self):
        self.peak = 0
        self.ctx = None

    def on_load(self, ctx):
        self.ctx = ctx

    def on_job_start(self, info):
        self.peak = max(self.peak, len(self.ctx.query_now("jobs")))


class Slow(BasePlugin):
    """A subscriber slower than the event rate."""

    name = "slow"
    events = "all"
    queue_size = 100_000

    def __init__(self):
        self.seen = 0

    def on_event(self, ev):
        time.sleep(0.0003)
        self.seen += 1


def build(lane, **kw):
    return Machine(
        program=PROGRAM,
        entry="main",
        libraries=[lib],
        bind={"work": Use(lane), "kir.std.*": Inline()},
        **kw,
    )


def test_target_load_paces_admission():
    # Resident jobs stay near the lane's target, not the source buffer's size
    lane = Lane([SimCall(name="svc", latency_ms=5, capacity=2, tail=0.0)], name="svc")
    peak = Peak()
    m = build(lane, admission=TargetLoad(lane, waiting=2), plugins=[peak])
    sink = Memory()
    with m:
        m.run(Iterable([{"x": i} for i in range(200)]), sink)
    assert len(sink.records) == 200 and all(r.ok for r in sink.records)
    assert peak.peak <= 2 + 2 + 1


def test_stop_delivers_every_queued_event():
    # A slow subscriber receives every event emitted before stop()
    lane = Lane([SimCall(name="svc2", latency_ms=1, capacity=64)], name="svc2")
    slow = Slow()
    m = build(lane, plugins=[slow], events="all")
    m.run(Iterable([{"x": i} for i in range(150)]), Memory())
    emitted = sum(m.bus.counts.values())
    m.stop()
    assert slow.seen >= emitted
