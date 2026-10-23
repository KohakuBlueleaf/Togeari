"""Loading while running: reply timeouts, concurrent loads; policy contracts at construction."""

import time

import pytest
from rt_helpers import machine, plugin

from togeari import ConstructionError, Lane, Use
from togeari.core.runtime.handles import Reply
from togeari.testing import SimCall, SimEngine

TWO = "@def (x: int)two(y):\n    (x)ident(y)\n"


class SlowStart(SimEngine):
    """A call unit whose start takes 0.3 s; counts its starts."""

    def __init__(self, **kw):
        super().__init__("call", **kw)
        self.starts = 0

    def start(self):
        self.starts += 1
        time.sleep(0.3)
        super().start()


# ---------------------------------------------------------------- load reply timeout
def test_load_waits_as_long_as_attach(monkeypatch):
    # load() waits the unit start timeout plus 30 s for its reply
    waits = []
    real = Reply.result

    def spy(self, timeout=30.0):
        waits.append(timeout)
        return real(self, timeout)

    monkeypatch.setattr(Reply, "result", spy)
    m = machine(start_timeout_s=200.0)
    try:
        assert m.load(TWO.replace("ident", "inc"), ["two"]) == ["two"]
        assert waits == [230.0]
    finally:
        m.stop()


# ---------------------------------------------------------------- concurrent loads
def test_two_loads_of_one_program_stage_it_once():
    # Two loads of one program share one region, one lane for the unit and one unit start
    u = SlowStart(name="late")
    m = machine(bind={"inc": Use(SimCall(name="initial")), "ident": Use(u)})
    try:
        assert u.starts == 0
        assert any(
            "ident" in warning.message and "matches no node" in warning.message
            for warning in m.warnings
        )
        first = m.command("load", program=TWO, entries=["two"])
        second = m.command("load", program=TWO, entries=["two"])
        assert first.result(10) == ["two"] and second.result(10) == ["two"]
        assert u.starts == 1
        assert sum(u is x for lane in m.lanes for x in lane.units) == 1
        assert len(m.code.regions) == 2
        assert m.submit({"x": 4}, entry="two").result(5).outputs == {"y": 4}
    finally:
        m.stop()


def test_a_load_staging_a_name_another_load_holds_is_refused():
    # An entry name staged by one load is refused to another; the first load's entry holds
    m = machine(bind={"inc": Use(SimCall(name="initial")), "ident": Use(SlowStart(name="late"))})
    try:
        first = m.command("load", program=TWO, entries=["two"])
        second = m.command("load", program=TWO.replace("ident", "inc"), entries=["two"])
        with pytest.raises(ConstructionError, match="another load"):
            second.result(10)
        assert first.result(10) == ["two"]
        assert m.submit({"x": 4}, entry="two").result(5).outputs == {"y": 4}
    finally:
        m.stop()


# ---------------------------------------------------------------- policy contracts
class OldStyle:
    def order(self, units, lane):
        return list(units)


def test_a_contributed_policy_missing_a_method_is_a_construction_problem():
    # A contributed policy without every contract method is a construction problem
    p = plugin("pol", contribute_policies=lambda: {"bad": OldStyle()})
    lane = Lane([SimCall(name="s")], select="bad")
    with pytest.raises(ConstructionError, match="admits"):
        machine(bind={"inc": Use(lane)}, plugins=[p])
