"""Disabling a plugin withdraws what it contributed; enabling it contributes again."""

from plugin_programs import PROGRAM, jobs

from conftest import build, run_jobs
from togeari.plugins import Counters, RunReport


def test_a_disabled_counters_is_no_longer_the_shared_one(tmp_path):
    counters = Counters(interval_s=0.05)
    m = build(PROGRAM, "main", n_units=2, latency_ms=10.0, plugins=[counters])
    try:
        assert "counters" in m.services and "counters" in m.queries

        # Disabled: its service and query leave the tables
        assert m.command("plugin_disable", name="counters").result(5) is True
        assert "counters" not in m.services and "counters" not in m.queries

        # A report added now runs its own counters and writes metrics from them
        report = RunReport(tmp_path / "report", compress=False, interval_s=0.05)
        m.command("plugin_add", plugin=report).result(5)
        assert report.own_counters and report.counters is not counters
        run_jobs(m, jobs(10))
    finally:
        m.stop()
    lines = (tmp_path / "report" / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines


def test_enabling_again_contributes_again():
    counters = Counters(interval_s=0.05)
    m = build(PROGRAM, "main", plugins=[counters])
    try:
        assert m.command("plugin_disable", name="counters").result(5) is True
        assert "counters" not in m.services
        assert m.command("plugin_enable", name="counters").result(5) is True
        assert m.services["counters"] is counters
        assert "counters" in m.queries
    finally:
        m.stop()
