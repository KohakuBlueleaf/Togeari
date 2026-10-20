"""Snapshots at the edges: inside an `each` iteration, with an undefined name, of another
layout version."""

import copy
import logging
import pickle
from types import SimpleNamespace

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.core.runtime.kir import UNDEF
from togeari.core.runtime.retire import snapshot
from togeari.stores import MemoryStore
from togeari.testing import SimCall

logging.disable(logging.CRITICAL)

LIB = NodeLibrary("snapedge")
FAIL = [False]
CALLS: list = []

# The last at-once iteration runs alone after the strand past the loop has ended
EACH = """
@def (items: list)main(out):
    out = 0
    (items)each(it, `body`)
    body:
        (it)slow(c)
        (c)isodd(d)
        (d)branch(`yes`, `no`)
        yes:
            (it)slow2(z)
            (z)isodd(d2)
            (d2)branch(`yes2`, `no2`)
            yes2:
                (z)slow3(w)
            no2:
        no:
"""

# `y` is written only on a branch the run does not take
UNDEFINED = """
a = 2
(a)slow(c)
(c)isodd(d)
(d)branch(`yes`, `no`)
yes:
    y = 1
no:
(a)slowb(e)
(e)iseven(f)
(f)branch(`p`, `q`)
p:
    (a)slow3(g)
q:
"""


@LIB.node()
def slow(it: int) -> int:
    CALLS.append(("slow", it))
    return it


@LIB.node()
def slowb(it: int) -> int:
    return it


@LIB.node()
def slow2(it: int) -> int:
    CALLS.append(("slow2", it))
    return it


@LIB.node()
def slow3(it: int) -> int:
    CALLS.append(("slow3", it))
    if FAIL[0]:
        raise RuntimeError("stop")
    return it


@LIB.node()
def isodd(c: int) -> bool:
    return c % 2 == 1


@LIB.node()
def iseven(c: int) -> bool:
    return c % 2 == 0


def run(
    program: str,
    entry,
    inputs: dict,
    store: MemoryStore,
    fail: bool,
    slowb_ms: float = 5,
    run_ahead: bool = False,
):
    FAIL[0] = fail
    CALLS.clear()

    # One snapshotting machine per job over the given store
    lane = Lane([SimCall(name="u", latency_ms=5, tail=0.0)])
    m = Machine(
        program=program,
        entry=entry,
        libraries=[LIB],
        bind={
            "slow": Use(lane),
            "slow2": Use(lane),
            "slow3": Use(lane),
            "slowb": Use(Lane([SimCall(name="b", latency_ms=slowb_ms, tail=0.0)])),
            "isodd": Inline(),
            "iseven": Inline(),
            "kir.std.*": Inline(),
        },
        store=store,
        snapshot_every=1,
        run_ahead=run_ahead,
    )

    try:
        return m.submit(inputs, key="k").result(20)
    finally:
        m.stop(drain=False, timeout=5)


def test_a_snapshot_inside_an_each_iteration_resumes_to_the_reference_outputs():
    reference = run(EACH, "main", {"items": [2, 4, 3]}, MemoryStore(), fail=False)
    assert reference.ok

    # The first run fails in the last iteration after a snapshot inside it
    store = MemoryStore()
    first = run(EACH, "main", {"items": [2, 4, 3]}, store, fail=True)
    assert not first.ok and first.stats.get("snapshots")

    # The resumed run ends like the reference and makes only the remaining call
    second = run(EACH, "main", {"items": [2, 4, 3]}, store, fail=False)
    assert second.ok, second.failure
    assert second.stats.get("resumed")
    assert second.outputs == reference.outputs
    assert CALLS == [("slow3", 3)]


def test_an_undefined_name_stays_undefined_across_a_resume():
    # Run-ahead past the first branch leaves `y` undefined in the snapshot's names
    reference = run(UNDEFINED, None, {}, MemoryStore(), fail=False, slowb_ms=60, run_ahead=True)
    assert reference.missing == ["y"]

    store = MemoryStore()
    first = run(UNDEFINED, None, {}, store, fail=True, slowb_ms=60, run_ahead=True)
    assert not first.ok and first.stats.get("snapshots")
    second = run(UNDEFINED, None, {}, store, fail=False, slowb_ms=60, run_ahead=True)
    assert second.ok and second.stats.get("resumed")
    assert second.missing == ["y"]
    assert second.outputs == reference.outputs


def test_the_undefined_value_pickles_and_copies_as_itself():
    assert pickle.loads(pickle.dumps(UNDEF)) is UNDEF
    assert copy.deepcopy(UNDEF) is UNDEF
    assert copy.copy([UNDEF])[0] is UNDEF


def test_a_snapshot_of_another_layout_version_is_no_snapshot():
    job = SimpleNamespace(key="k")
    other = pickle.dumps({"version": snapshot.VERSION + 1, "pc": 0})
    assert snapshot.restore(job, other) is False
    unversioned = pickle.dumps({"pc": 0, "frames": [], "values": {}})
    assert snapshot.restore(job, unversioned) is False
