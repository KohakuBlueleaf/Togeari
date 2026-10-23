"""Results are kept for reuse only when a handler refetches the squashed code, and within the
per-job byte cap (``squash_reuse_bytes``)."""

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.testing.sim import SimEngine

LIB = NodeLibrary("scope")
DIGESTS: list[int] = []


class Counted:
    """A value that counts how often it is digested."""

    def __init__(self, v: int):
        self.v = v

    def __togeari_digest__(self) -> bytes:
        DIGESTS.append(self.v)
        return f"counted:{self.v}".encode()


@LIB.node()
def boom(x: int) -> int:
    if x == 1:
        raise ValueError("boom")
    return x


@LIB.node()
def wrap(x: int) -> object:
    return Counted(x)


@LIB.node()
def work(o: object) -> int:
    return o.v * 10


@LIB.node()
def blob1(x: int) -> bytes:
    return b"1" * 1000


@LIB.node()
def blob2(x: int) -> bytes:
    return b"2" * 1000


def unit(name: str, latency_ms: float) -> SimEngine:
    return SimEngine("call", name=name, latency_ms=latency_ms, tail=0.0, capacity=16)


def run(program: str, inline: tuple, lanes: dict, x: int = 1, **knobs):
    deterministic_nodes = {"wrap", "work", "blob1", "blob2"}
    bind = {
        name: Use(
            Lane([unit(name, ms)], name=name),
            deterministic=True if name in deterministic_nodes else None,
        )
        for name, ms in lanes.items()
    }
    bind.update(
        {
            name: Inline(deterministic=True if name in deterministic_nodes else None)
            for name in inline
        }
    )
    bind["kir.std.*"] = Inline()
    m = Machine(program=program, entry="main", libraries=[LIB], bind=bind, **knobs)
    try:
        return m.submit({"x": x}, key="k").result(10)
    finally:
        m.stop(drain=False, timeout=5)


# `work` finishes long before `boom` fails
UNCAUGHT = """
@def (x: int)main(a, b):
    (x)boom(a)
    (x)wrap(o)
    (o)work(b)
"""

CAUGHT = """
@def (x: int)main(a, b):
    @try:
        (x)boom(a)
    @except err:
        a = 0
    (x)wrap(o)
    (o)work(b)
"""


def test_a_fault_no_handler_takes_keeps_nothing():
    DIGESTS.clear()
    rec = run(UNCAUGHT, ("wrap",), {"boom": 300, "work": 5})
    assert not rec.ok and rec.failure["node"] == "boom"

    # Nothing digested the squashed `work`'s inputs to keep its result
    assert DIGESTS == []


def test_a_fault_a_handler_takes_keeps_the_results_for_the_refetch():
    DIGESTS.clear()
    rec = run(CAUGHT, ("wrap",), {"boom": 300, "work": 5})
    assert rec.ok and rec.outputs == {"a": 0, "b": 10}

    # `wrap` and `work` both completed before the fault; both are taken by the refetch
    assert rec.stats["reused"] == 2
    assert DIGESTS


BLOBS = """
@def (x: int)main(y, b, c):
    @try:
        (x)boom(a)
    @except err:
        a = 0
    (x)blob1(b)
    (x)blob2(c)
    y = a
"""


def test_the_kept_results_stay_within_the_byte_cap():
    lanes = {"boom": 300, "blob1": 5, "blob2": 5}
    both = run(BLOBS, (), lanes, squash_reuse_bytes=None)
    assert both.ok and both.stats["reused"] == 2

    # Room for one 1000-byte result: the older one is dropped
    one = run(BLOBS, (), lanes, squash_reuse_bytes=1500)
    assert one.ok and one.stats["reused"] == 1

    # A result larger than the cap is never kept
    none = run(BLOBS, (), lanes, squash_reuse_bytes=500)
    assert none.ok and none.stats["reused"] == 0
    assert none.outputs == {"y": 0, "b": b"1" * 1000, "c": b"2" * 1000}
