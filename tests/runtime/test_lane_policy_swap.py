"""`set_lane_policy`: a lane's former or selector swapped while the machine runs."""

import pytest
from rt_helpers import machine, plugin

from togeari import Lane, Use
from togeari.policies import LeastLoaded, SizeOrWait
from togeari.testing import SimBatch, SimCall


def batch_lane() -> Lane:
    unit = SimBatch(name="b", latency_ms=5, tail=0.0, max_batch=16, capacity=1)
    return Lane([unit], name="batch", former=SizeOrWait(2, 200))


def burst(m, n: int, start: int = 0) -> list:
    handles = [m.submit({"x": i}) for i in range(start, start + n)]
    return [h.result(10) for h in handles]


class OnlyUnit(LeastLoaded):
    def __init__(self, name: str):
        self.name = name

    def admits(self, item, unit, lane):
        return unit.name == self.name


class NoTake:
    def key(self, item, unit):
        return None


def test_a_swapped_former_changes_the_batches_the_lane_forms():
    lane = batch_lane()
    m = machine(bind={"inc": Use(lane)})
    try:
        assert all(r.ok for r in burst(m, 32))
        assert max(lane.batch_sizes) <= 2

        # The new former forms batches above the old size, up to its own
        old = m.command("set_lane_policy", lane="batch", policy="former", value=SizeOrWait(8, 200))
        assert old.result(5) == "SizeOrWait"
        before = dict(lane.batch_sizes)
        recs = burst(m, 32, start=100)
        assert [r.outputs["y"] for r in recs] == list(range(101, 133))
        after = {k: v - before.get(k, 0) for k, v in lane.batch_sizes.items()}
        formed = [size for size, n in after.items() if n > 0]
        assert max(formed) > 2 and max(formed) <= 8
    finally:
        m.stop()


def test_a_swapped_selector_routes_the_next_items():
    units = [SimCall(name=n, latency_ms=1, tail=0.0, capacity=8) for n in ("u1", "u2")]
    lane = Lane(units, name="pair")
    m = machine(bind={"inc": Use(lane)})
    try:
        assert all(r.ok for r in burst(m, 20))
        m.command("set_lane_policy", lane="pair", policy="select", value=OnlyUnit("u2")).result(5)
        done = {st.unit.name: st.items_done for st in lane.state.values()}
        assert all(r.ok for r in burst(m, 20, start=50))
        delta = {st.unit.name: st.items_done - done[st.unit.name] for st in lane.state.values()}
        assert delta == {"u1": 0, "u2": 20}
    finally:
        m.stop()


def test_a_contributed_former_may_be_named():
    big = SizeOrWait(8, 200)
    provider = plugin("provider", contribute_policies=lambda: {"big": big})
    lane = batch_lane()
    m = machine(bind={"inc": Use(lane)}, plugins=[provider])
    try:
        m.command("set_lane_policy", lane="batch", policy="former", value="big").result(5)
        assert lane.former is big
    finally:
        m.stop()


@pytest.mark.parametrize(
    "args, error",
    [
        ({"lane": "nope", "policy": "former", "value": None}, KeyError),
        ({"lane": "batch", "policy": "forming", "value": "early"}, ValueError),
        ({"lane": "batch", "policy": "former", "value": NoTake()}, ValueError),
        ({"lane": "batch", "policy": "former", "value": None}, ValueError),
        ({"lane": "batch", "policy": "former", "value": "unknown"}, ValueError),
        ({"lane": "batch", "policy": "select", "value": None}, ValueError),
    ],
    ids=["no-lane", "not-a-policy", "contract", "batch-needs-former", "unknown-name", "no-select"],
)
def test_a_refused_policy_keeps_the_old_one(args, error):
    lane = batch_lane()
    former, select = lane.former, lane.select
    m = machine(bind={"inc": Use(lane)})
    try:
        with pytest.raises(error):
            m.command("set_lane_policy", **args).result(5)
        assert lane.former is former and lane.select is select
        assert all(r.ok for r in burst(m, 8))
    finally:
        m.stop()


def test_a_call_lane_takes_no_former():
    lane = Lane([SimCall(name="c", latency_ms=1, tail=0.0)], name="call")
    m = machine(bind={"inc": Use(lane)})
    try:
        with pytest.raises(ValueError, match="exactly on batch lanes"):
            m.command(
                "set_lane_policy", lane="call", policy="former", value=SizeOrWait(4, 10)
            ).result(5)
        assert lane.former is None
    finally:
        m.stop()
