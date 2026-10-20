"""Per-event isolation in the dispatcher, stop() under a live dispatcher, the source limit."""

import logging

import pytest
from rt_helpers import ENTERED, RELEASE, in_thread, machine, plugin, sim_lane, wait_for

from togeari import Inline, Use
from togeari.io import Memory
from togeari.testing import SimCall

logging.disable(logging.CRITICAL)


# ---------------------------------------------------------------- event isolation
def test_a_raising_posted_call_is_counted_and_the_machine_goes_on():
    # A posted call that raises is a machine error; the dispatcher keeps serving jobs
    m = machine()
    try:
        assert m.submit({"x": 1}).result(5).ok

        def bad():
            raise RuntimeError("call bug")

        m.dispatcher.inbox.put(("call", bad))
        m.dispatcher.inbox.put(("call", bad, None))
        assert m.submit({"x": 2}).result(5).outputs == {"y": 3}
        assert m.inspect("stats").result()["machine_errors"] == 2
    finally:
        m.stop()


def test_a_raising_lane_results_fails_its_items_not_the_machine():
    # A raising lane.results() fails that submission's items only
    lane = sim_lane()
    m = machine(bind={"inc": Use(lane)})
    real = lane.results
    try:

        def broken(sub, results, now):
            lane.results = real
            raise RuntimeError("results bug")

        lane.results = broken
        rec = m.submit({"x": 1}).result(5)
        assert not rec.ok and "results bug" in rec.failure["message"]
        assert m.submit({"x": 2}).result(5).outputs == {"y": 3}
        assert m.inspect("stats").result()["machine_errors"] == 1
    finally:
        m.stop()


def test_an_attach_completion_that_raises_fails_the_attach():
    # The attach caller gets the completion's error; the machine goes on
    lane = sim_lane()
    m = machine(bind={"inc": Use(lane)}, start_timeout_s=1.0)
    try:
        assert m.submit({"x": 1}).result(5).ok

        def broken(unit):
            raise RuntimeError("attach bug")

        lane.attach = broken
        finished, got = in_thread(lambda: m.attach(lane, SimCall(name="extra")), 5)
        assert finished and isinstance(got, RuntimeError) and "attach bug" in str(got)
        assert m.submit({"x": 2}).result(5).ok
    finally:
        m.stop()


# ---------------------------------------------------------------- stop under a live loop
def test_stop_marks_a_stuck_dispatcher_dead_before_tearing_down():
    # Within its timeout, stop() marks a stuck dispatcher dead before plugins unload, and the
    # resident job is answered
    ENTERED.clear()
    RELEASE.clear()
    seen = []
    watch = plugin("watch", on_unload=lambda: seen.append(m.dispatcher.dead is not None))
    m = machine(
        program="@def (x: int)main(y):\n    (x)block(y)\n",
        bind={"block": Inline()},
        plugins=[watch],
    )
    try:
        h = m.submit({"x": 1})
        assert wait_for(ENTERED.is_set)
        finished, got = in_thread(lambda: m.stop(drain=False, timeout=0.3), 5)
        assert finished and got is None
        assert h.result(1).failure["kind"] == "DispatcherDied"
        assert seen == [True]
    finally:
        RELEASE.set()


# ---------------------------------------------------------------- the source limit
def test_the_reader_takes_no_item_past_the_limit():
    # The source is pulled exactly `limit` times
    pulled = []

    def source():
        for i in range(10):
            pulled.append(i)
            yield {"x": i}

    m = machine()
    try:
        sink = Memory()
        m.run(source(), sink, limit=3)
        assert len(sink.records) == 3 and pulled == [0, 1, 2]
    finally:
        m.stop()


@pytest.fixture(autouse=True)
def _released():
    yield
    RELEASE.set()
