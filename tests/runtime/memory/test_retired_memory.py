"""A retired statement holds neither its inputs nor its work item's payload while its job goes
on."""

import gc
import weakref

from kohakunode import NodeLibrary
from rt_helpers import ENTERED, RELEASE, sim_lane, wait_for

from togeari import Inline, Machine, Use

LIB = NodeLibrary("retired")
REFS: list = []


class Box:
    """A value whose lifetime the test watches."""

    def __init__(self, v: int):
        self.v = v


@LIB.node()
def make(x: int) -> object:
    box = Box(x)
    REFS.append(weakref.ref(box))
    return box


@LIB.node()
def use(b: object) -> int:
    return b.v + 1


@LIB.node()
def hold(x: int) -> int:
    ENTERED.set()
    RELEASE.wait(10)
    return x


# `use` reads the box on a lane, the name is rebound, then `hold` keeps the job resident
PROGRAM = """
@def (x: int)main(y, c):
    (x)make(b)
    (b)use(c)
    b = 0
    (x)hold(y)
"""


def test_a_retired_lane_call_lets_go_of_its_inputs_and_payload():
    REFS.clear()
    ENTERED.clear()
    RELEASE.clear()
    m = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        bind={
            "make": Inline(),
            "use": Use(sim_lane("u", tail=0.0)),
            "hold": Use(sim_lane("h", tail=0.0)),
            "kir.std.*": Inline(),
        },
    )
    try:
        h = m.submit({"x": 1}, key="k")
        assert ENTERED.wait(5)

        # `use` retires behind nothing older; its box is then held by no one
        def box_freed() -> bool:
            gc.collect()
            return REFS[0]() is None

        assert wait_for(box_freed, 3)
        assert not h.done()
    finally:
        RELEASE.set()
        m.stop()
    assert h.result(5).outputs == {"y": 1, "c": 2}
