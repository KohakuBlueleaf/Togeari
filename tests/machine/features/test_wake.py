"""Engines wake the machine when their capacity rises without a completion: no other event is
needed for queued work to go."""

import logging
import threading
import time

from feature_nodes import LIB

from conftest import bounded
from togeari import EngineBase, Inline, Lane, Machine, Ok, Use
from togeari.engines.python import PythonCallEngine

logging.disable(logging.CRITICAL)

DOUBLE = "@def (x: int)main(y):\n    (x)double(y)\n"
NAP = "@def (x: int, ms: int)main(y):\n    (x, ms)nap(y)\n"


def machine(program: str, bind: dict, **kw) -> Machine:
    bind.setdefault("kir.std.*", Inline())
    return Machine(program=program, entry="main", libraries=[LIB], bind=bind, **kw)


class Gated(EngineBase):
    """Capacity set by the test; answers each item on a thread of its own."""

    def __init__(self):
        super().__init__("gated")
        self.cap = 0
        self.inflight = 0
        self.subs: list = []

    def capacity(self):
        return self.cap - self.inflight

    def submit(self, work, done):
        items = work if isinstance(work, list) else [work]
        self.subs.append(items)
        self.inflight += 1

        def answer():
            results = [Ok(it.finish(it.payload().run()), True) for it in items]
            self.inflight -= 1
            done(len(self.subs), results)

        threading.Thread(target=answer, daemon=True).start()
        return len(self.subs)


def queued(m) -> int:
    return sum(lane["queue"] for lane in m.inspect("lanes").result())


def test_a_wake_after_capacity_rises_submits_the_queued_item():
    unit = Gated()
    m = machine(DOUBLE, {"double": Use(Lane([unit]))})
    try:
        h = m.submit({"x": 4})

        def wait_queued():
            while queued(m) != 1:
                time.sleep(0.005)

        bounded(m, wait_queued, 10)
        assert unit.subs == [] and not h.done()

        # Capacity rises with no completion; the wake alone gets the item submitted
        unit.cap = 1
        unit.wake()
        rec = bounded(m, lambda: h.result(10), 15)
        assert rec.outputs == {"y": 8} and len(unit.subs) == 1
    finally:
        m.stop(drain=False, timeout=5)


def test_a_thread_worker_freed_after_a_timeout_runs_the_next_job_unprompted():
    engine = PythonCallEngine(workers=1, mode="thread")
    m = machine(NAP, {"nap": Use(Lane([engine], timeout_s=0.3))})
    try:
        first = m.submit({"x": 1, "ms": 1500})
        second = m.submit({"x": 2, "ms": 1})
        assert bounded(m, lambda: first.result(10), 15).failure["kind"] == "ItemTimeout"

        # The slot frees when the timed-out call returns; nothing else is submitted
        rec = bounded(m, lambda: second.result(10), 15)
        assert rec.ok and rec.outputs == {"y": 2}
    finally:
        m.stop(drain=False, timeout=5)
