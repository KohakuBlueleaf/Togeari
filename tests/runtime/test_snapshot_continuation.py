"""A resumed job continues exactly where its snapshot left it: the calls after the resume are
the remaining ones, none repeated before the snapshot point and none skipped."""

import time

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.stores import MemoryStore
from togeari.testing import SimCall

LIB = NodeLibrary("snapcont")
CALLS: list[int] = []
STOP_AT: list = [None]

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


@LIB.node()
def step(x: int) -> int:
    CALLS.append(x)
    if STOP_AT[0] is not None and x == STOP_AT[0]:
        raise RuntimeError("stop here")
    return x + 1


def machine(store: MemoryStore, latency_ms: float, **knobs) -> Machine:
    lane = Lane([SimCall(name="steps", latency_ms=latency_ms, tail=0.0)])
    return Machine(
        program=LOOP,
        entry="main",
        libraries=[LIB],
        bind={"step": Use(lane), "kir.std.*": Inline()},
        store=store,
        **knobs,
    )


def resume(store: MemoryStore) -> list[int]:
    """The calls of a second machine over ``store`` running the same job to its end."""
    CALLS.clear()
    STOP_AT[0] = None
    m = machine(store, latency_ms=0.2, snapshot_every=5)
    try:
        rec = m.submit({"n": 50}, key="k").result(20)
    finally:
        m.stop(drain=False, timeout=5)
    assert rec.ok and rec.outputs == {"x": 50}, rec.failure
    assert rec.stats.get("resumed")
    return list(CALLS)


def test_a_job_resumed_after_a_failure_makes_exactly_the_remaining_calls():
    store = MemoryStore()

    # First run: every call up to the failing one, snapshots on the way
    CALLS.clear()
    STOP_AT[0] = 30
    m = machine(store, latency_ms=0.2, snapshot_every=5)
    try:
        first = m.submit({"n": 50}, key="k").result(20)
    finally:
        m.stop(drain=False, timeout=5)
    made = list(CALLS)
    assert not first.ok
    assert made == list(range(31))

    # Second run: from the last snapshot's value to the end, in order
    calls = resume(store)
    k = calls[0]
    assert 0 < k <= 30, calls
    assert calls == list(range(k, 50))


def test_a_job_resumed_after_a_stop_makes_exactly_the_remaining_calls():
    store = MemoryStore()

    # First run: cancelled by the stop once some snapshot exists and 10 calls were made
    CALLS.clear()
    STOP_AT[0] = None
    m = machine(store, latency_ms=20, snapshot_every=5)
    try:
        h = m.submit({"n": 50}, key="k")
        end = time.monotonic() + 10
        while (len(CALLS) < 10 or not len(store)) and time.monotonic() < end:
            time.sleep(0.005)
    finally:
        m.stop(drain=False, timeout=5)
    assert h.result(5).failure["kind"] == "Cancelled"
    made = list(CALLS)
    assert made == list(range(len(made))) and len(made) >= 10

    # Second run: no gap after the snapshot point, nothing past the first run's last call skipped
    calls = resume(store)
    k = calls[0]
    assert 0 < k <= len(made), (made, calls)
    assert calls == list(range(k, 50))
