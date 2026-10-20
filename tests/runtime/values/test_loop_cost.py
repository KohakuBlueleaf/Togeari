"""Loops written with `jump` keep bounded order keys and frames, `mutates` versions are folded at
retire, and every run conforms to the reference (inline, lane-bound and parked conditions)."""

import pytest
from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.conformance import conform
from togeari.core.runtime.retire.retire import fold_versions
from togeari.core.runtime.state import Inflight, Slot, SlotState, effective
from togeari.testing import SimCall

LIB = NodeLibrary("loops")


@LIB.node()
def inc(x: int) -> int:
    return x + 1


@LIB.node()
def mklist(x: int) -> list:
    return [x]


@LIB.node(mutates=["xs"], outputs=["n"])
def push(xs: list) -> int:
    xs.append(len(xs))
    return len(xs)


@LIB.node()
def odd(x: int) -> bool:
    return x % 2 == 1


# A do-while loop that grows a list in place, with an `if` inside its body
LOOP = """
@def (k: int)main(xs, x, t):
    x = 0
    t = 0
    (x)mklist(xs)
    ()jump(`top`)
    top:
        (xs)push(m)
        (x)odd(o)
        (o)branch(`yes`, `no`)
        yes:
            (t, x)add(t)
        no:
        (x)inc(x)
        (x, k)less(more)
        (more)branch(`again`, `done`)
        again:
            ()jump(`top`)
        done:
"""

# An outer loop whose body runs an inner loop
NESTED = """
@def (k: int)main(t):
    t = 0
    i = 0
    ()jump(`outer`)
    outer:
        j = 0
        ()jump(`inner`)
        inner:
            (t, j)add(t)
            (j)inc(j)
            (j, 3)less(go)
            (go)branch(`back`, `out`)
            back:
                ()jump(`inner`)
            out:
        (i)inc(i)
        (i, k)less(more)
        (more)branch(`again`, `done`)
        again:
            ()jump(`outer`)
        done:
"""


def lane(name: str, ms: float = 1.0) -> Lane:
    return Lane([SimCall(name=name, latency_ms=ms, tail=0.0, capacity=8)], name=name)


def machine(program: str, bind: dict, **knobs) -> Machine:
    return Machine(
        program=program,
        entry="main",
        libraries=[LIB],
        bind={**bind, "kir.std.*": Inline()},
        record=1.0,
        **knobs,
    )


def run(program: str, bind: dict, k: int, **knobs):
    m = machine(program, bind, **knobs)
    keys: list[tuple] = []
    m.bus.listen(lambda kind, fields: keys.append(fields["key"]), kinds=["stmt.fetch"])
    jp = m.code.entries["main"].region.job
    try:
        rec = m.submit({"k": k}).result(60)
    finally:
        m.stop()
    return rec, jp, keys


INLINE = {"inc": Inline(), "mklist": Inline(), "push": Inline(), "odd": Inline()}

# Bindings over the inline ones: lane-bound calls, and a loop condition that is pending
BINDS = {
    "inline": lambda: {},
    "lanes": lambda: {"inc": Use(lane("inc")), "push": Use(lane("push"))},
    "slow-condition": lambda: {"inc": Use(lane("inc", 3.0)), "odd": Use(lane("odd"))},
}


@pytest.mark.parametrize("run_ahead", [True, False], ids=["run-ahead", "no-run-ahead"])
@pytest.mark.parametrize("bind", BINDS.values(), ids=BINDS.keys())
def test_a_jump_loop_conforms_with_bounded_keys(bind, run_ahead):
    k = 120
    rec, jp, keys = run(LOOP, {**INLINE, **bind()}, k, run_ahead=run_ahead)
    assert rec.ok, rec.failure
    assert rec.outputs["x"] == k and rec.outputs["xs"] == list(range(k + 1))
    for mode in ("replay", "reexecute"):
        v = conform(jp, rec, {"k": k}, mode=mode)
        assert v.ok, (mode, v.reason)

    # Keys are distinct and stay short however many iterations ran
    assert len(set(keys)) == len(keys)
    assert max(len(key) for key in keys) <= 5


@pytest.mark.parametrize("bind", [{}, {"inc": Use(lane("inc"))}], ids=["inline", "lane"])
def test_a_nested_jump_loop_conforms_with_bounded_keys(bind):
    k = 60
    rec, jp, keys = run(NESTED, {"inc": Inline(), **bind}, k)
    assert rec.ok, rec.failure
    assert rec.outputs["t"] == 3 * k
    v = conform(jp, rec, {"k": k}, mode="reexecute")
    assert v.ok, v.reason
    assert max(len(key) for key in keys) <= 7


def test_retire_folds_versions_older_than_the_retired_call():
    base = Slot("v0", SlotState.READY)
    versions = {}
    for n in range(1, 5):
        v = Slot(f"v{n}", SlotState.READY)
        versions[n] = v
        base.muts.append(((n,), v))
    versions[4].state = SlotState.SQUASHED

    # Readers younger than the retired call see the same version before and after the fold
    seen = {key: effective(base, key) for key in [(2, 5), (3, 1), (9,)]}
    x = Inflight(None, None, (2,))
    x.versions = {"xs": (base, versions[2])}
    fold_versions(x)
    assert [k for k, _ in base.muts] == [(2,), (3,)]
    assert {key: effective(base, key) for key in seen} == seen
