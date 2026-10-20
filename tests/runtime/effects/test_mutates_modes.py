"""The three `mutates` modes: when a `mutates` call copies its input, and when it waits for
older work (spec/03 §3.7)."""

import time

import pytest
from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.testing import SimCall

LIB = NodeLibrary("mutmodes")
TIMES: dict[str, float] = {}


@LIB.node()
def slow(x: int) -> int:
    TIMES["slow"] = time.monotonic()
    return x


@LIB.node()
def mklist(x: int) -> list:
    return [x]


@LIB.node(mutates=["xs"], outputs=["n"])
def push(xs: list) -> int:
    TIMES["push"] = time.monotonic()
    xs.append(99)
    return len(xs)


# An older call still in flight when `push` is ready: `push` is speculative
SPECULATIVE = "@def (x: int)main(a, n):\n    (x)slow(a)\n    (x)mklist(xs)\n    (xs)push(n)\n"

# Nothing older in flight when `push` is ready
SETTLED = "@def (x: int)main(n):\n    (x)mklist(xs)\n    (xs)push(n)\n"


def run(program: str, mode: str):
    TIMES.clear()
    m = Machine(
        program=program,
        entry="main",
        libraries=[LIB],
        bind={
            "slow": Use(Lane([SimCall(name="slow", latency_ms=200, tail=0.0)])),
            "mklist": Inline(),
            "push": Inline(),
            "kir.std.*": Inline(),
        },
        mutates=mode,
    )
    try:
        return m.submit({"x": 1}).result(10)
    finally:
        m.stop(drain=False, timeout=5)


@pytest.mark.parametrize(
    ("mode", "copies"),
    [("copy-if-speculative", 1), ("copy-always", 1), ("wait", 0)],
)
def test_a_speculative_mutates_call_copies_or_waits_per_mode(mode, copies):
    rec = run(SPECULATIVE, mode)
    assert rec.ok and rec.outputs == {"a": 1, "n": 2}, rec.failure
    assert rec.stats.get("mutates_copies", 0) == copies, rec.stats

    # A copy runs at once, before the older call wrote back; `wait` runs after it
    match mode:
        case "wait":
            assert TIMES["push"] >= TIMES["slow"]
        case _:
            assert TIMES["push"] < TIMES["slow"]


@pytest.mark.parametrize(
    ("mode", "copies"),
    [("copy-if-speculative", 0), ("copy-always", 1), ("wait", 0)],
)
def test_a_settled_mutates_call_copies_only_under_copy_always(mode, copies):
    rec = run(SETTLED, mode)
    assert rec.ok and rec.outputs == {"n": 2}, rec.failure
    assert rec.stats.get("mutates_copies", 0) == copies, rec.stats
