"""Job bytes count what a job holds: a value's bytes leave the count once no slot holds it, so a
loop that rebinds one name stays bounded while the bytes it produced grow with every turn."""

import gc

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.io import Iterable, Memory
from togeari.policies import MemoryBound
from togeari.testing import SimCall

LIB = NodeLibrary("resident")
PAD = 10_000

LOOP = """
@def (n: int)main(x):
    x = 0
    ()jump(`top`)
    top:
        (x)pad(blob)
        (x)step(x)
        (x, n)less(more)
        (more)branch(`again`, `done`)
        again:
            ()jump(`top`)
        done:
"""


@LIB.node()
def pad(x: int) -> bytes:
    return bytes(PAD)


@LIB.node()
def step(x: int) -> int:
    return x + 1


def machine(pad_binding, **kw) -> Machine:
    return Machine(
        program=LOOP,
        entry="main",
        libraries=[LIB],
        bind={"pad": pad_binding, "step": Inline(), "kir.std.*": Inline()},
        **kw,
    )


def test_a_loop_rebinding_one_name_holds_a_bounded_number_of_bytes():
    turns = 400
    m = machine(Inline())
    try:
        rec = m.submit({"n": turns}, key="loop").result(30)
    finally:
        m.stop()
    assert rec.ok and rec.outputs == {"x": turns}, rec.failure

    # Every turn produced a value; the job never held more than a window's worth of them
    assert rec.stats["bytes_produced"] == turns * PAD
    assert rec.stats["bytes_peak"] <= 20 * PAD, rec.stats["bytes_peak"]

    # Nothing is left on the pool once the job ended
    assert m.dispatcher.pool_bytes == 0


def test_bytes_released_on_another_thread_reach_the_pool():
    # Values held by slots on a lane: their releases go through the queue, then the pool
    lane = Lane([SimCall(name="pads", latency_ms=1, tail=0.0)])
    m = machine(Use(lane))
    try:
        rec = m.submit({"n": 50}, key="lane").result(30)
        gc.collect()
        sig = m.signals()
    finally:
        m.stop()
    assert rec.ok, rec.failure
    assert rec.stats["bytes_produced"] == 50 * PAD
    assert rec.stats["bytes_peak"] < 50 * PAD
    assert sig["bytes"] == 0


def test_the_bytes_memory_bound_reads_stay_bounded_during_a_long_job():
    # A job producing 300 values of 10 kB: the pool bytes an admission rule sees mid-run stay
    # at a few values, and a 200 kB cap keeps admitting a second job
    lane = Lane([SimCall(name="pads", latency_ms=1, tail=0.0)])
    seen: list[int] = []
    m = machine(Use(lane), admission=MemoryBound(200_000))
    try:
        sink = Memory()
        h = m.submit({"n": 300}, key="long")
        while not h.done():
            seen.append(m.signals()["bytes"])
        m.run(Iterable([{"n": 5}]), sink)
        rec = h.result(30)
    finally:
        m.stop()
    assert rec.ok and rec.stats["bytes_produced"] == 300 * PAD
    assert seen and max(seen) <= 20 * PAD, max(seen)
    assert [r.ok for r in sink.records] == [True]
