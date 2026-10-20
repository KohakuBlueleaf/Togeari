"""Construction checks: lane forming values, store modes and snapshots without a machine store."""

import pytest
from rt_helpers import machine, plugin, sim_lane

from togeari import ConstructionError, Lane, Use
from togeari.stores import MemoryStore
from togeari.testing import SimCall


def test_a_lane_refuses_an_unknown_forming_value():
    with pytest.raises(ValueError, match="forming"):
        Lane([SimCall(name="s")], forming="bogus")

    # Both known values are taken, also when set later
    lane = Lane([SimCall(name="s")], forming="early")
    lane.forming = "late"
    assert lane.forming == "late"
    with pytest.raises(ValueError, match="forming"):
        lane.forming = "eager"
    assert lane.forming == "late"


class NoTake:
    """An object offered as a former that lacks most of the contract."""

    def key(self, item, unit):
        return None


def test_a_named_former_missing_a_method_is_a_construction_problem():
    p = plugin("pol", contribute_policies=lambda: {"f": NoTake()})
    lane = Lane([SimCall(name="s")], former="f")
    with pytest.raises(ConstructionError, match=r"former NoTake lacks .*take \(Former contract\)"):
        machine(bind={"inc": Use(lane)}, plugins=[p])


def test_a_store_mode_without_a_machine_store_is_a_construction_problem():
    with pytest.raises(ConstructionError, match="store='content'.*no store"):
        machine(bind={"inc": Use(sim_lane(), store="content")})
    with pytest.raises(ConstructionError, match="store='positional'.*no store"):
        machine(bind={"inc": Use(sim_lane(), store="positional")})

    # With a store both build
    m = machine(bind={"inc": Use(sim_lane(), store="content")}, store=MemoryStore())
    m.stop()


def test_snapshots_without_a_machine_store_are_a_construction_problem():
    with pytest.raises(ConstructionError, match="snapshot_every needs a machine store"):
        machine(snapshot_every=5)
    machine(snapshot_every=5, store=MemoryStore()).stop()
    machine(snapshot_every=None).stop()


def test_snapshots_set_at_runtime_without_a_store_are_refused():
    m = machine()
    try:
        with pytest.raises(ValueError, match="snapshot_every needs a machine store"):
            m.command("set_knob", name="snapshot_every", value=5).result(5)
        assert m.inspect("knobs").result(5)["snapshot_every"] is None
    finally:
        m.stop()
