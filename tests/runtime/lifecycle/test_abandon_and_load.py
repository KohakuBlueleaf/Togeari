"""A run ends when its dispatcher is abandoned; a failed load leaves code memory as it was;
the top level takes no inputs; a raising issue priority falls back."""

import logging
import threading

import pytest
from rt_helpers import ENTERED, LIB, RELEASE, machine, sim_lane

from togeari import ConstructionError, DispatcherDied, Inline, Machine, Use
from togeari.io import Iterable, Memory
from togeari.policies import OldestJobFirst

logging.disable(logging.CRITICAL)

BLOCK = "@def (x: int)main(y):\n    (x)block(y)\n"
TOP = "x = 1\n(x)inc(y)\n"


def test_a_run_ends_as_soon_as_its_stuck_dispatcher_is_abandoned():
    ENTERED.clear()
    RELEASE.clear()
    m = machine(program=BLOCK, bind={"block": Inline()})
    result: dict = {}

    def run_it():
        try:
            result["summary"] = m.run(Iterable([{"x": 1}]), Memory())
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    runner = threading.Thread(target=run_it, daemon=True)
    try:
        runner.start()
        assert ENTERED.wait(5)

        # The stop gives up on the stuck loop; the run ends without waiting for the node
        m.stop(drain=False, timeout=0.3)
        runner.join(3)
        assert not runner.is_alive()
        assert isinstance(result.get("error"), DispatcherDied)
    finally:
        RELEASE.set()


def test_a_failed_load_leaves_code_memory_as_it_was():
    m = machine()
    try:
        before = len(m.code.mem)

        # `ident` is a library node with no binding: the load fails after decoding
        with pytest.raises(ConstructionError):
            m.load("@def (x: int)other(y):\n    (x)ident(y)\n", entries=["other"])
        assert len(m.code.mem) == before
        assert m.submit({"x": 1}).result(5).ok
    finally:
        m.stop()


def test_the_top_level_refuses_inputs():
    m = Machine(
        program=TOP,
        entry=None,
        libraries=[LIB],
        bind={"inc": Use(sim_lane()), "kir.std.*": Inline()},
    )
    try:
        assert m.submit({}).result(5).ok
        rec = m.submit({"x": 1}).result(5)
        assert not rec.ok
        assert "the top level: unknown inputs ['x']" in rec.failure["message"]
    finally:
        m.stop()


class PriorityRaises(OldestJobFirst):
    def priority(self, st):
        raise RuntimeError("priority bug")


def test_a_raising_issue_priority_falls_back_to_zero_and_is_counted():
    m = machine(issue=PriorityRaises())
    try:
        recs = [m.submit({"x": i}).result(5) for i in range(4)]
        assert [r.outputs for r in recs] == [{"y": i + 1} for i in range(4)]
        assert m.dispatcher.dead is None
        assert m.dispatcher.stats["policy_errors"] >= 4
    finally:
        m.stop()
