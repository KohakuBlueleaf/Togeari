"""Snapshots and resume, debug checks, queries and commands, stores, construction errors."""

import pytest
from kohakunode import NodeLibrary

from togeari import ConstructionError, Inline, Lane, Machine, Use
from togeari.io import Iterable, Memory
from togeari.stores import MemoryStore
from togeari.testing import SimCall

LIB = NodeLibrary("feat")
CALLS: list[int] = []
STOP_AT = [None]


@LIB.node()
def step(x: int) -> int:
    CALLS.append(x)
    if STOP_AT[0] is not None and x == STOP_AT[0]:
        raise RuntimeError("stop here")
    return x + 1


@LIB.node()
def sneaky(xs: list) -> int:
    xs.append(0)
    return len(xs)


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


def machine(store=None, **knobs):
    return Machine(
        program=LOOP,
        entry="main",
        libraries=[LIB],
        bind={"step": Use(SimCall(name="steps", latency_ms=0.2)), "kir.std.*": Inline()},
        store=store,
        **knobs,
    )


def test_snapshot_resume_continues_where_it_stopped():
    store = MemoryStore()

    # First run fails halfway; snapshots were taken on the way
    CALLS.clear()
    STOP_AT[0] = 30
    m = machine(store, snapshot_every=5)
    sink = Memory()
    try:
        m.run(Iterable([{"n": 50}], key=None), sink)
    finally:
        m.stop()
    assert not sink.records[0].ok
    first = len(CALLS)

    # Second run resumes from the last snapshot and finishes
    CALLS.clear()
    STOP_AT[0] = None
    m = machine(store, snapshot_every=5)
    sink = Memory()
    try:
        m.run(Iterable([{"n": 50}]), sink)
    finally:
        m.stop()
    rec = sink.records[0]
    assert rec.ok and rec.outputs == {"x": 50}
    assert rec.stats.get("resumed")
    assert len(CALLS) < 50 - 20, (first, len(CALLS))


def test_fingerprint_inputs_catches_undeclared_mutation():
    m = Machine(
        program="@def (n: int)main(k):\n    xs = [1]\n    (xs)sneaky(k)\n",
        entry="main",
        libraries=[LIB],
        bind={"sneaky": Inline(), "kir.std.*": Inline()},
        fingerprint_inputs=True,
    )
    try:
        rec = m.submit({"n": 1}).result(10)
        assert not rec.ok and rec.failure["kind"] == "UndeclaredMutation"
    finally:
        m.stop()


def test_queries_and_commands():
    m = machine()
    try:
        assert m.inspect("programs").result()[0]["entry"] == "main"
        old = m.command("set_knob", name="window", value=8).result()
        assert old == 64 and m.inspect("knobs").result()["window"] == 8
        h = m.submit({"n": 5})
        assert h.result(10).outputs == {"x": 5}
        assert m.inspect("stats").result()["calls"] >= 5
        with pytest.raises(KeyError):
            m.command("set_knob", name="nope", value=1).result()
    finally:
        m.stop()


def test_load_a_second_program_while_running():
    m = machine()
    try:
        m.load("@def (a: int)twice(b):\n    (a)step(t)\n    (t)step(b)\n", ["twice"])
        assert m.submit({"a": 1}, entry="twice").result(10).outputs == {"b": 3}
        assert m.submit({"n": 2}, entry="main").result(10).outputs == {"x": 2}
    finally:
        m.stop()


def test_construction_lists_every_problem():
    with pytest.raises(ConstructionError) as e:
        Machine(
            program="@def (a)main(b):\n    (a)nope(b)\n    (b)step(c)\n",
            entry="main",
            libraries=[LIB],
            bind={"st*": Use(Lane([SimCall(name="x")])), "s*": Inline()},
            bogus_knob=1,
        )
    text = str(e.value)
    assert "bogus_knob" in text
    assert "nope" in text


def test_content_store_serves_repeated_inputs():
    store = MemoryStore()
    lane = Lane([SimCall(name="s", latency_ms=0.2)])
    m = Machine(
        program="@def (a: int)main(b):\n    (a)step(b)\n",
        entry="main",
        libraries=[LIB],
        bind={"step": Use(lane, store="content")},
        store=store,
    )
    try:
        for _ in range(2):
            sink = Memory()
            m.run(Iterable([{"a": 1}] * 10), sink)
            assert all(r.outputs == {"b": 2} for r in sink.records)
        assert m.inspect("stats").result()["store_hits"] >= 10
    finally:
        m.stop()
