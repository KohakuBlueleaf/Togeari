"""Machine events to plugins: unit states gated by lane, `machine.error` when the dispatcher
dies."""

import logging
import queue

import pytest
from feature_nodes import LIB

from conftest import CountOutage, bounded
from togeari import BasePlugin, DispatcherDied, Inline, Lane, Machine, Retry, Use
from togeari.io import Iterable, Memory
from togeari.testing import SimCall

logging.disable(logging.CRITICAL)

TWO_LANES = "@def (x: int)main(y):\n    (x)double(a)\n    (a)step(y)\n"


class States(BasePlugin):
    """The (unit, state) pairs it saw; gated to ``lanes`` when given."""

    def __init__(self, name: str, lanes: list | None = None):
        self.name = name
        self.applies_to = {"lanes": lanes} if lanes is not None else None
        self.seen: list = []

    def on_unit_state(self, unit, state):
        self.seen.append((unit, state))


def test_unit_states_reach_only_plugins_of_their_lane():
    lanes = {}
    for name in ("a", "b"):
        down = CountOutage("call", down_at=1, polls=1, name=f"{name}0", latency_ms=1)
        good = SimCall(name=f"{name}1", latency_ms=1)
        lanes[name] = Lane([down, good], retry=Retry(max=5, backoff_ms=1), name=name)
    gated, free = States("gated", ["a"]), States("free")
    m = Machine(
        program=TWO_LANES,
        entry="main",
        libraries=[LIB],
        bind={"double": Use(lanes["a"]), "step": Use(lanes["b"]), "kir.std.*": Inline()},
        plugins=[gated, free],
    )
    try:
        sink = Memory()
        bounded(m, lambda: m.run(Iterable([{"x": i} for i in range(20)]), sink), 30)
        assert all(r.ok for r in sink.records)
    finally:
        m.stop(drain=False, timeout=5)

    # Both outages were seen by the ungated plugin, only lane a's by the gated one
    assert ("a0", "down") in free.seen and ("b0", "down") in free.seen
    assert ("a0", "down") in gated.seen
    assert all(unit.startswith("a") for unit, _ in gated.seen), gated.seen


class Errors(BasePlugin):
    name = "errors"
    events = "all"

    def __init__(self):
        self.q: queue.Queue = queue.Queue()

    def on_event(self, ev):
        if ev.kind == "machine.error":
            self.q.put(ev.fields)


def test_machine_error_reaches_a_subscriber_when_the_dispatcher_dies():
    lane = Lane([SimCall(name="s", latency_ms=1)])
    sub = Errors()
    m = Machine(
        program=TWO_LANES,
        entry="main",
        libraries=[LIB],
        bind={"double": Use(lane), "step": Use(lane), "kir.std.*": Inline()},
        plugins=[sub],
    )
    try:

        def broken(now):
            raise RuntimeError("lane bug")

        lane.pump = broken
        rec = bounded(m, lambda: m.submit({"x": 1}).result(10), 15)
        assert rec.failure["kind"] == "DispatcherDied"
        fields = sub.q.get(timeout=5)
        assert "lane bug" in fields["error"]
        with pytest.raises(DispatcherDied):
            m.submit({"x": 2})
    finally:
        m.stop(drain=False, timeout=5)
