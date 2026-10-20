"""The plugin context's public reads: copies of the machine's state, kind-filtered listeners."""

import re
from pathlib import Path

from kohakunode.testing import LIB
from plugin_programs import PROGRAM, jobs

import togeari.plugins
from conftest import build, run_jobs
from togeari.core.plugin.base import BasePlugin
from togeari.plugins import Counters
from togeari.plugins.metrics.counters import LISTENED


class Reader(BasePlugin):
    """Keeps its context and the kinds its listener received."""

    name = "reader"

    def __init__(self):
        self.ctx = None
        self.kinds: list[str] = []
        self.views: dict = {}

    def on_load(self, ctx):
        self.ctx = ctx
        ctx.listen(lambda kind, fields: self.kinds.append(kind), ["job.admit"])

    def on_job_end(self, record):
        self.kinds.append("running" if self.ctx.machine_running() else "stopped")

    def on_machine_stop(self):
        ctx = self.ctx
        self.views = {
            "stats": ctx.stats(),
            "lanes": ctx.lanes(loads=True),
            "units": ctx.units(),
            "events": ctx.event_counts(),
            "resident": ctx.resident_jobs(),
            "running": ctx.machine_running(),
        }


def test_context_reads_are_copies_of_the_machine_state():
    reader = Reader()
    m = build(PROGRAM, "main", plugins=[reader])
    try:
        run_jobs(m, jobs(8))
    finally:
        m.stop()
    ctx, views = reader.ctx, reader.views
    assert not ctx.machine_running() and views["running"] is False

    # Dispatcher counters: equal to the machine's, and a copy
    stats = ctx.stats()
    assert stats["calls"] == m.dispatcher.stats["calls"] > 0
    stats["phase_s"]["fetch"] = -1.0
    assert m.dispatcher.stats["phase_s"].get("fetch") != -1.0

    # Lanes and units: the lanes' own numbers, with loads on request
    lanes = {ln["name"]: ln for ln in views["lanes"]}
    assert set(lanes) == {ln.name for ln in m.lanes}
    for lane in m.lanes:
        view = lanes[lane.name]
        assert view["stats"] == lane.stats and view["config"]["former"] is lane.former
        done = sum(st.items_done for st in lane.state.values())
        assert sum(u["done"] for u in view["units"]) == done
        assert all("running" in u["load"] for u in view["units"])
    assert [u["name"] for u in views["units"]] == [
        st.unit.name for lane in m.lanes for st in lane.state.values()
    ]
    assert "load" not in views["units"][0]

    # Events: the bus's counts; the listener got its one kind only
    assert views["events"]["job.admit"] == 8 and views["resident"] == 0
    assert sorted(reader.kinds) == ["job.admit"] * 8 + ["running"] * 8
    assert ctx.libraries == [LIB]


def test_counters_listen_to_their_kinds_only():
    counters = Counters(interval_s=60.0)
    m = build(PROGRAM, "main", plugins=[counters])
    try:
        assert counters._listen not in m.bus.listeners
        assert all(counters._listen in m.bus.by_kind[k] for k in LISTENED)
        run_jobs(m, jobs(5))
    finally:
        m.stop()
    snap = counters.latest
    assert snap["jobs"]["admitted"] == 5 and snap["jobs"]["ended"] == 5
    assert snap["dispatcher"]["events"]["job.admit"] == 5
    assert sum(snap["work"]["calls_by_node"].values()) == m.dispatcher.stats["calls"]


def test_builtin_plugins_reach_the_machine_through_the_context_only():
    root = Path(togeari.plugins.__file__).parent
    reach = re.compile(r"\._m\b")
    found = [
        f"{path.relative_to(root)}:{n}"
        for path in sorted(root.rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if reach.search(line)
    ]
    assert found == []
