"""The ``gc_threshold`` knob: asked of the process collector while loops run, then withdrawn."""

import gc
import json

import pytest
from rt_helpers import ENTERED, RELEASE, machine, wait_for

from togeari import ConstructionError, Inline
from togeari.core.construct.knobs import KNOB_DEFAULTS
from togeari.core.runtime import collector

BLOCK = "@def (x: int)main(y):\n    (x)block(y)\n"


@pytest.fixture(autouse=True)
def _process_threshold():
    base = gc.get_threshold()
    yield base
    RELEASE.set()
    gc.set_threshold(*base)


def test_the_threshold_holds_while_running_and_returns_at_stop(_process_threshold):
    base = _process_threshold
    m = machine()
    try:
        assert m.submit({"x": 1}).result(5).ok
        assert gc.get_threshold() == KNOB_DEFAULTS["gc_threshold"]

        # None withdraws the ask: the process's own threshold
        m.command("set_knob", name="gc_threshold", value=None).result(5)
        assert m.submit({"x": 2}).result(5).ok
        assert gc.get_threshold() == base

        # A list is stored as a tuple; a short ask keeps the process's later values
        m.command("set_knob", name="gc_threshold", value=[40_000]).result(5)
        assert m.submit({"x": 3}).result(5).ok
        assert gc.get_threshold() == (40_000, *base[1:])
    finally:
        m.stop()
    assert gc.get_threshold() == base
    assert collector.held() == {}


def test_re_asking_keeps_one_ask_per_loop():
    m = machine(gc_threshold=(30_000, 5, 5))
    try:
        for want in [(31_000, 5, 5), (32_000, 6, 6), (30_000, 5, 5)]:
            m.command("set_knob", name="gc_threshold", value=want).result(5)
            assert m.submit({"x": 1}).result(5).ok
            assert gc.get_threshold() == want
            assert list(collector.held().values()) == [want]
    finally:
        m.stop()


def test_overlapping_machines_in_either_stop_order(_process_threshold):
    base = _process_threshold
    for first in ("older", "newer"):
        older = machine(gc_threshold=(30_000, 5, 5))
        newer = machine(gc_threshold=(60_000,))
        try:
            assert older.submit({"x": 1}).result(5).ok
            assert newer.submit({"x": 1}).result(5).ok

            # The short ask is padded from the process threshold, not from the older ask
            assert gc.get_threshold() == (60_000, *base[1:])

            match first:
                case "older":
                    older.stop()
                    assert gc.get_threshold() == (60_000, *base[1:])
                case "newer":
                    newer.stop()
                    assert gc.get_threshold() == (30_000, 5, 5)
        finally:
            older.stop()
            newer.stop()
        assert gc.get_threshold() == base
        assert collector.held() == {}


def test_a_threshold_set_from_outside_while_running_is_kept_after_stop():
    m = machine()
    try:
        assert m.submit({"x": 1}).result(5).ok
        gc.set_threshold(1234, 7, 7)
    finally:
        m.stop()
    assert gc.get_threshold() == (1234, 7, 7)


def test_an_abandoned_loop_withdraws_its_ask_while_still_stuck(_process_threshold):
    base = _process_threshold
    ENTERED.clear()
    RELEASE.clear()
    m = machine(program=BLOCK, bind={"block": Inline()}, gc_threshold=(45_000, 5, 5))
    try:
        h = m.submit({"x": 1})
        assert ENTERED.wait(5)
        assert gc.get_threshold() == (45_000, 5, 5)

        # The loop is stuck inside the inline call; stop gives up on it
        m.stop(drain=False, timeout=0.3)
        assert m.dispatcher.dead is not None
        assert gc.get_threshold() == base
        assert collector.held() == {}
        assert h.result(1).failure["kind"] == "DispatcherDied"
    finally:
        RELEASE.set()

    # The released loop ends without asking again
    assert wait_for(lambda: not m.dispatcher.running)
    assert gc.get_threshold() == base
    assert collector.held() == {}


def test_the_knobs_query_shows_the_threshold_as_a_list():
    m = machine(gc_threshold=(50_000, 20, 100))
    try:
        knobs = m.inspect("knobs").result(5)
    finally:
        m.stop()
    assert knobs["gc_threshold"] == [50_000, 20, 100]
    assert json.loads(json.dumps(knobs))["gc_threshold"] == [50_000, 20, 100]


@pytest.mark.parametrize("bad", [(0,), (0, 10, 10), (-1,), (1, 2, 3, 4), (), (1.5,), [5]])
def test_a_bad_threshold_is_refused(bad):
    if isinstance(bad, list):
        # A list is taken as a tuple: valid
        machine(gc_threshold=bad).stop()
        return
    with pytest.raises(ConstructionError, match="gc_threshold"):
        machine(gc_threshold=bad)
