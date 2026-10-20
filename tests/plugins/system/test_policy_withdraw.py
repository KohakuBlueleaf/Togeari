"""A withdrawn policy leaves the lanes that use it; a plugin whose load fails withdraws its
policies."""

from rt_helpers import machine

from togeari import BasePlugin, Lane, Use
from togeari.policies import LeastLoaded
from togeari.testing import SimCall


class Pinned(LeastLoaded):
    """The plugin's selector, told apart by type."""


class Offers(BasePlugin):
    name = "offers"

    def __init__(self, fail_load: bool = False):
        self.fail_load = fail_load
        self.selector = Pinned()

    def contribute_policies(self) -> dict:
        return {"pinned": self.selector}

    def on_load(self, ctx):
        if self.fail_load:
            raise RuntimeError("load failed")


def pinned_lane() -> Lane:
    return Lane([SimCall(name="c", latency_ms=1, tail=0.0)], name="call", select="pinned")


def test_a_lane_leaves_a_withdrawn_selector_for_the_default():
    plugin, lane = Offers(), pinned_lane()
    m = machine(bind={"inc": Use(lane)}, plugins=[plugin])
    try:
        assert lane.select is plugin.selector
        assert m.command("plugin_disable", name="offers").result(5) is True
        assert type(lane.select) is LeastLoaded
        assert "pinned" not in m.contributed_policies

        # The machine keeps running on the default
        assert m.submit({"x": 1}).result(5).outputs == {"y": 2}
    finally:
        m.stop()


def test_a_plugin_whose_load_fails_withdraws_its_policies():
    plugin, lane = Offers(fail_load=True), pinned_lane()
    m = machine(bind={"inc": Use(lane)}, plugins=[plugin])
    try:
        assert "pinned" not in m.contributed_policies
        assert type(lane.select) is LeastLoaded
        assert m.submit({"x": 1}).result(5).outputs == {"y": 2}
    finally:
        m.stop()
