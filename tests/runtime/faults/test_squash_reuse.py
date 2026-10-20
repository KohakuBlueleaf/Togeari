"""After a taken fault, a refetched call whose inputs are unchanged takes the result its squashed
run already made (``squash_reuse``); `mutates` and `ordered` calls run again; reuse off re-issues.
"""

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.conformance import conform
from togeari.stores import MemoryStore
from togeari.testing.sim import SimEngine

LIB = NodeLibrary("reuse")
EFFECTS: list[int] = []


@LIB.node()
def boom(x: int) -> int:
    if x == 1:
        raise ValueError("boom")
    return x


@LIB.node()
def work(x: int) -> int:
    return x * 10


@LIB.node()
def other(x: int) -> int:
    return x * 100


@LIB.node()
def mklist(x: int) -> list:
    return [x]


@LIB.node(mutates=["xs"], outputs=["n"])
def push(xs: list) -> int:
    xs.append(99)
    return len(xs)


@LIB.node(ordered=True)
def note(x: int) -> int:
    EFFECTS.append(x)
    return x


# `boom` answers late; the calls after the `@try` finish before it fails
PROGRAM = """
@def (x: int)main(y, z, n):
    @try:
        (x)boom(a)
    @except err:
        a = 0
    (x)work(b)
    (x)other(c)
    (b, c)add(z)
    (a, z)add(y)
    (x)mklist(xs)
    (xs)push(n)
    (x)note(e)
"""

SLOW_MS = 300


class Counting(SimEngine):
    """A sim call unit with a flat latency."""

    def __init__(self, name: str, latency_ms: float):
        super().__init__("call", name=name, latency_ms=latency_ms, tail=0.0, capacity=16)


def build(positional: tuple = (), **knobs) -> tuple[Machine, dict[str, Counting]]:
    units = {
        "boom": Counting("boom", SLOW_MS),
        "work": Counting("work", 5),
        "other": Counting("other", 5),
        "mklist": Counting("mklist", 5),
        "push": Counting("push", 5),
    }
    bind = {
        name: Use(Lane([u], name=name), store="positional" if name in positional else None)
        for name, u in units.items()
    }
    m = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        bind={**bind, "note": Inline(), "kir.std.*": Inline()},
        record=1.0,
        **knobs,
    )
    return m, units


def submits(unit: Counting) -> int:
    return unit.counts["submits"]


def run(x: int, positional: tuple = (), **knobs):
    EFFECTS.clear()
    m, units = build(positional, **knobs)
    jp = m.code.entries["main"].region.job
    try:
        rec = m.submit({"x": x}, key=f"k{x}").result(10)
    finally:
        m.stop(drain=False, timeout=5)
    return rec, units, jp


def test_a_refetched_call_with_the_same_inputs_takes_its_squashed_result():
    rec, units, jp = run(1)
    assert rec.ok, rec.failure
    assert rec.outputs == {"y": 110, "z": 110, "n": 2}

    # `work` and `other` finished before the fault and are not sent again
    assert submits(units["work"]) == 1
    assert submits(units["other"]) == 1
    assert rec.stats["squashed"] >= 2
    assert rec.stats["reused"] >= 2

    # The record holds each call once, as the reference makes them
    for mode in ("replay", "reexecute"):
        v = conform(jp, rec, {"x": 1}, mode=mode, pure={"boom", "work", "other", "mklist"})
        assert v.ok, (mode, v.reason)


def test_mutates_and_ordered_calls_run_again_after_the_fault():
    rec, units, _ = run(1)
    assert rec.ok, rec.failure

    # The `mutates` call ran on a copy before the fault and runs again on the list after it
    assert submits(units["push"]) == 2
    assert rec.outputs["n"] == 2

    # The `ordered` call waited for the fault and ran once
    assert EFFECTS == [1]


def test_without_a_fault_nothing_is_kept_or_reused():
    rec, units, _ = run(2)
    assert rec.ok and rec.outputs == {"y": 222, "z": 220, "n": 2}
    assert rec.stats["reused"] == 0
    assert submits(units["work"]) == 1
    assert submits(units["push"]) == 1


def test_reuse_off_sends_the_refetched_calls_again():
    rec, units, jp = run(1, squash_reuse=0)
    assert rec.ok and rec.outputs == {"y": 110, "z": 110, "n": 2}
    assert rec.stats["reused"] == 0
    assert submits(units["work"]) == 2
    assert submits(units["other"]) == 2
    v = conform(jp, rec, {"x": 1}, mode="reexecute", pure={"boom", "work", "other", "mklist"})
    assert v.ok, v.reason


def test_a_positional_binding_is_sent_again():
    # A positional result belongs to its order key, which the refetch after the handler moves
    rec, units, _ = run(1, positional=("work",), store=MemoryStore())
    assert rec.ok and rec.outputs == {"y": 110, "z": 110, "n": 2}
    assert submits(units["work"]) == 2
    assert submits(units["other"]) == 1


def test_the_kept_results_are_capped_per_job():
    rec, units, _ = run(1, squash_reuse=1)
    assert rec.ok and rec.outputs == {"y": 110, "z": 110, "n": 2}

    # One kept result of the three finished calls: the other two are sent again
    again = submits(units["work"]) + submits(units["other"]) + submits(units["mklist"]) - 3
    assert rec.stats["reused"] == 1
    assert again == 2


@LIB.node()
def fast(x: int) -> int:
    return x + 5


# `work` reads `a`, which `fast` writes before the fault and the handler writes again after it
CHANGED = """
@def (x: int)main(b):
    @try:
        (x)fast(a)
        (x)boom(q)
    @except err:
        a = 0
    (a)work(b)
"""


def test_a_refetched_call_whose_inputs_changed_runs_again():
    units = {"boom": Counting("boom", SLOW_MS), "fast": Counting("fast", 5)}
    units["work"] = Counting("work", 5)
    bind = {name: Use(Lane([u], name=name)) for name, u in units.items()}
    m = Machine(
        program=CHANGED,
        entry="main",
        libraries=[LIB],
        bind={**bind, "kir.std.*": Inline()},
        record=1.0,
    )
    jp = m.code.entries["main"].region.job
    try:
        rec = m.submit({"x": 1}, key="k").result(10)
    finally:
        m.stop(drain=False, timeout=5)
    assert rec.ok, rec.failure

    # The speculative `work` saw a = 6; after the handler a = 0, so its kept result is not taken
    assert rec.outputs == {"b": 0}
    assert submits(units["work"]) == 2
    assert rec.stats["reused"] == 0
    v = conform(jp, rec, {"x": 1}, mode="reexecute", pure={"boom", "fast", "work"})
    assert v.ok, v.reason
