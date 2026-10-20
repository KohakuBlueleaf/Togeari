"""Lanes: multi-issue, failover on unit errors, retries, timeouts, attach / detach, forming
modes."""

import time

from kohakunode import NodeLibrary

from conftest import CountOutage
from togeari import Lane, Machine, Retry, Use
from togeari.io import Iterable, Memory
from togeari.policies import Affinity, RoundRobin, SizeOrWait, Throughput
from togeari.testing import SimBatch, SimCall, SimStream

LIB = NodeLibrary("lanes")


@LIB.node()
def double(x: int) -> int:
    return x * 2


@LIB.node()
def slow_double(x: int) -> int:
    time.sleep(0.2)
    return x * 2


PROGRAM = """
@def (x: int)main(y):
    (x)double(a)
    (a)double(b)
    (b)double(y)
"""


def run(machine, n=30):
    sink = Memory()
    machine.run(Iterable([{"x": i} for i in range(n)]), sink)
    return sink.by_key()


def check_outputs(recs, n=30):
    assert len(recs) == n
    for k, r in recs.items():
        assert r.ok, r.failure
        assert r.outputs == {"y": int(k) * 8}


def test_multi_issue_uses_every_unit():
    units = [SimCall(name=f"u{i}", latency_ms=2, capacity=2) for i in range(3)]
    m = Machine(program=PROGRAM, entry="main", libraries=[LIB], bind={"double": Use(Lane(units))})
    try:
        check_outputs(run(m))
        lanes = m.inspect("lanes").result()
        done = [u["done"] for u in lanes[0]["units"]]
        assert all(d > 0 for d in done), done
    finally:
        m.stop()


def test_unit_outage_fails_over_without_job_errors():
    good = SimCall(name="good", latency_ms=2, capacity=4)
    flaky = CountOutage("call", name="down", latency_ms=2, capacity=4)
    lane = Lane([flaky, good], retry=Retry(max=5, backoff_ms=1))
    m = Machine(program=PROGRAM, entry="main", libraries=[LIB], bind={"double": Use(lane)})
    try:
        # Down after start, and for every health poll of the run
        flaky.go_down(polls=10**9)
        check_outputs(run(m))
        units = {u["name"]: u for u in m.inspect("units").result()}
        assert units["down"]["state"] == "down" and flaky.counts["outage_rejects"] > 0
    finally:
        m.stop()


def test_unit_outage_at_start_is_a_construction_error():
    dead = SimCall(name="dead", outages=[(0.0, 60.0)])
    try:
        Machine(
            program=PROGRAM,
            entry="main",
            libraries=[LIB],
            bind={"double": Use(Lane([dead]))},
            start_timeout_s=0.2,
        )
    except Exception as exc:  # noqa: BLE001
        assert "dead" in str(exc)
    else:
        raise AssertionError("expected a construction error")


def test_item_timeout_is_an_item_error():
    m = Machine(
        program="@def (x: int)main(y):\n    (x)slow_double(y)\n",
        entry="main",
        libraries=[LIB],
        bind={"slow_double": Use(Lane([SimCall(name="t", latency_ms=1)], timeout_s=0.05))},
    )
    try:
        recs = run(m, 3)
        assert all(not r.ok and r.failure["kind"] == "ItemTimeout" for r in recs.values())
    finally:
        m.stop()


def test_forming_modes_and_selectors_give_same_results():
    for forming in ("late", "early"):
        for select in (RoundRobin(), Throughput(), Affinity()):
            lane = Lane(
                [SimBatch(name=f"b{i}", latency_ms=1, speed=1 + i) for i in range(3)],
                former=SizeOrWait(8, 2),
                forming=forming,
                select=select,
            )
            m = Machine(program=PROGRAM, entry="main", libraries=[LIB], bind={"double": Use(lane)})
            try:
                check_outputs(run(m))
            finally:
                m.stop()


def test_attach_and_detach_while_running():
    first = SimStream(name="first", latency_ms=5)
    lane = Lane([first], name="work")
    m = Machine(program=PROGRAM, entry="main", libraries=[LIB], bind={"double": Use(lane)})
    try:
        handles = [m.submit({"x": i}, key=str(i)) for i in range(40)]
        m.attach("work", SimStream(name="second", latency_ms=5))
        m.detach("first", drain=False)
        recs = {h.key: h.result(30) for h in handles}
        check_outputs(recs, 40)
        units = {u["name"]: u for u in m.inspect("units").result()}
        assert units["first"]["state"] == "detached"
        assert units["second"]["done"] > 0
    finally:
        m.stop()
