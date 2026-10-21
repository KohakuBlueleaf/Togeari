"""A fault inside `@try`: younger work is squashed and its call cancelled, the handler runs, the
job ends without waiting for the cancelled call, and a younger `ordered` effect never runs."""

import time

import pytest
from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.conformance import conform
from togeari.testing.sim import SimEngine

LIB = NodeLibrary("faults")
EFFECTS: list[int] = []


@LIB.node()
def boom(x: int) -> int:
    if x == 1:
        raise ValueError(f"boom({x})")
    return x


@LIB.node()
def slow(x: int) -> int:
    return x


@LIB.node(ordered=True)
def note(x: int) -> int:
    EFFECTS.append(x)
    return x


PROGRAM = """
@def (x: int)main(y):
    @try:
        (x)boom(a)
        (x)slow(b)
        (x)note(n)
        y = a
    @except err:
        y = -1
"""

# The slow call takes 0.75 to 2.25 s; the failing one 50 to 150 ms
SLOW_MS = 1500


class CountingSim(SimEngine):
    """A sim call unit that counts the cancels it receives."""

    def __init__(self, **kw):
        super().__init__("call", tail=0.0, **kw)
        self.cancels = 0

    def cancel(self, ticket):
        self.cancels += 1
        super().cancel(ticket)


def build() -> tuple[Machine, CountingSim]:
    slow_unit = CountingSim(name="slow", latency_ms=SLOW_MS)
    fast_unit = SimEngine("call", name="boom", latency_ms=100, tail=0.0)
    m = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        bind={
            "boom": Use(Lane([fast_unit])),
            "slow": Use(Lane([slow_unit])),
            "note": Inline(),
            "kir.std.*": Inline(),
        },
        record=1.0,
    )
    return m, slow_unit


@pytest.mark.serial
def test_a_taken_fault_squashes_and_cancels_younger_work():
    EFFECTS.clear()
    m, slow_unit = build()
    jp = m.code.entries["main"].region.job
    try:
        t0 = time.monotonic()
        rec = m.submit({"x": 1}, key="fault").result(10)
        dt = time.monotonic() - t0

        # The handler's value, without waiting for the slow call
        assert rec.ok and rec.outputs == {"y": -1}, rec.failure
        assert dt < 0.6, dt

        # The slow call was squashed and its submission cancelled; the effect never ran
        assert rec.stats["squashed"] >= 2, rec.stats
        assert slow_unit.cancels == 1
        assert EFFECTS == []

        # The reference takes the same path over the same calls
        v = conform(jp, rec, {"x": 1}, mode="reexecute", pure={"boom", "slow"})
        assert v.ok, v.reason

        # Without a fault the same program runs the effect once and keeps the call
        ok = m.submit({"x": 2}, key="clean").result(10)
        assert ok.ok and ok.outputs == {"y": 2}
        assert EFFECTS == [2]
        assert slow_unit.cancels == 1
    finally:
        m.stop(drain=False, timeout=5)
