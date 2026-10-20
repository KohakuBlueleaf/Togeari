"""Hooks: a pass-through plugin changes no result; hook time is visible; enable / disable work."""

from collections import Counter

from kohakunode.testing import entry_program
from plugin_programs import PROGRAM, jobs

from conftest import build, run_jobs
from togeari import BasePlugin
from togeari.conformance import conform

HOT_HOOKS = (
    "pre_admit",
    "vote_admit",
    "on_job_start",
    "on_job_end",
    "pre_submit",
    "post_result",
    "pre_sink",
)


class PassThrough(BasePlugin):
    """Overrides every hook and changes nothing."""

    name = "passthrough"

    def __init__(self) -> None:
        self.calls: Counter = Counter()
        self.loads = 0

    def on_load(self, ctx) -> None:
        self.loads += 1

    def pre_admit(self, item):
        self.calls["pre_admit"] += 1

    def vote_admit(self, signals):
        self.calls["vote_admit"] += 1
        return True

    def on_job_start(self, job):
        self.calls["on_job_start"] += 1

    def on_job_end(self, record):
        self.calls["on_job_end"] += 1

    def pre_submit(self, item, unit, lane):
        self.calls["pre_submit"] += 1

    def post_result(self, result, node, job):
        self.calls["post_result"] += 1

    def on_unit_state(self, unit, state):
        self.calls["on_unit_state"] += 1

    def on_fault(self, fault):
        self.calls["on_fault"] += 1

    def pre_sink(self, record):
        self.calls["pre_sink"] += 1


def test_pass_through_plugin_keeps_conformance():
    seen: Counter = Counter()
    for seed in range(8):
        p = entry_program(seed, jobs=4)
        plugin = PassThrough()
        m = build(
            p.text, p.entry, seed=seed, n_units=2, window=64, latency_ms=0.5, plugins=[plugin]
        )
        try:
            sink = run_jobs(m, p.inputs)
            jp = m.code.entries[p.entry].region.job
        finally:
            m.stop()
        assert len(sink.records) == len(p.inputs)
        for rec in sink.records:
            v = conform(jp, rec, p.inputs[int(rec.key)])
            assert v.ok, f"seed {seed} job {rec.key}: {v.reason}"
        assert plugin.calls["pre_sink"] == len(p.inputs)
        seen.update(plugin.calls)

    # Every hot-path hook was reached
    for hook in HOT_HOOKS:
        assert seen[hook] > 0, hook


def test_hook_time_shows_in_plugins_query():
    plugin = PassThrough()
    m = build(PROGRAM, "main", plugins=[plugin])
    try:
        run_jobs(m, jobs(10))
        rows = {r["name"]: r for r in m.inspect("plugins").result()}
    finally:
        m.stop()
    row = rows["passthrough"]
    assert row["enabled"]
    for hook in HOT_HOOKS:
        assert row["hook_s"].get(hook, 0.0) > 0.0, hook


def test_plugin_disable_and_enable():
    n = 6
    plugin = PassThrough()
    m = build(PROGRAM, "main", plugins=[plugin])
    try:
        run_jobs(m, jobs(n))
        assert plugin.calls["pre_sink"] == n

        # Disabled: no hook fires
        assert m.command("plugin_disable", name="passthrough").result() is True
        assert m.command("plugin_disable", name="passthrough").result() is False
        rows = {r["name"]: r for r in m.inspect("plugins").result()}
        assert rows["passthrough"]["enabled"] is False
        before = dict(plugin.calls)
        sink = run_jobs(m, jobs(n))
        assert len(sink.records) == n
        assert dict(plugin.calls) == before

        # Enabled again: on_load runs again and hooks fire
        assert m.command("plugin_enable", name="passthrough").result() is True
        assert plugin.loads == 2
        run_jobs(m, jobs(n))
        assert plugin.calls["pre_sink"] == 2 * n
    finally:
        m.stop()
