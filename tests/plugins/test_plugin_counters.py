"""Counters: every job, call and submission is counted exactly once across the snapshots."""

import pytest
from plugin_programs import PROGRAM, jobs

from conftest import build, run_jobs
from togeari.plugins import Counters
from togeari.plugins.metrics import bucket_us, hist_percentile


def test_hist_percentile_on_known_histogram():
    # 90 cycles under 1 us, 9 in [512, 1024) us, 1 in [8192, 16384) us
    hist = {0: 90, 10: 9, 14: 1}
    assert hist_percentile(hist, 0.5) == 1.0
    assert hist_percentile(hist, 0.95) == 1024.0
    assert hist_percentile(hist, 0.995) == 16384.0
    assert hist_percentile({}, 0.5) is None
    assert bucket_us(14) == 16384.0


def test_counters_account_for_every_job_and_call():
    counters = Counters(interval_s=0.05)
    snaps: list[dict] = []
    counters.watchers.append(snaps.append)
    m = build(PROGRAM, "main", n_units=2, latency_ms=2.0, plugins=[counters])
    try:
        sink = run_jobs(m, jobs(40))
        latest = m.inspect("counters").result()
        calls = m.dispatcher.stats["calls"]
        submitted = {ln.name: ln.stats["submitted"] for ln in m.lanes}
        units = {u.name for ln in m.lanes for u in ln.units}
    finally:
        m.stop()
    cycles = sum(m.dispatcher.stats["cycle_hist"].values())
    run_max_us = m.dispatcher.stats["cycle_max_s"] * 1e6

    # Several snapshots, the last one taken at stop
    assert len(sink.records) == 40
    assert len(snaps) >= 2
    assert set(latest) >= {"dispatcher", "lanes", "units", "jobs", "work"}

    # Sums over the snapshots equal the machine's totals
    assert sum(s["jobs"]["ended"] for s in snaps) == 40
    assert sum(s["jobs"]["admitted"] for s in snaps) == 40
    assert sum(s["jobs"]["ok"] + s["jobs"]["failed"] for s in snaps) == 40
    assert sum(sum(s["work"]["calls_by_node"].values()) for s in snaps) == calls
    assert sum(s["work"]["calls_per_s"] * s["dt"] for s in snaps) == pytest.approx(calls)
    for lane, n in submitted.items():
        assert sum(s["lanes"][lane]["submitted"] for s in snaps) == n
    assert set(snaps[-1]["units"]) == units

    # Rates are fractions and per-node calls name the program's nodes
    for s in snaps:
        assert 0.0 <= s["dispatcher"]["busy"] <= 1.0 + 1e-6
        for u in s["units"].values():
            assert 0.0 <= u["busy"] <= 1.0 + 1e-6
    nodes = set().union(*(s["work"]["calls_by_node"] for s in snaps))
    assert {"inc", "mix", "small"} <= nodes

    # Cycle jitter: per-sample histograms add up to every cycle, p50 <= p99 <= max; a bucket's
    # upper bound is under twice the longest cycle of the whole run
    assert sum(s["dispatcher"]["cycle"]["n"] for s in snaps) == cycles
    for s in snaps:
        c = s["dispatcher"]["cycle"]
        if c["n"]:
            assert c["p50_us"] <= c["p99_us"] <= c["max_us"] <= 2 * max(run_max_us, 1.0)

    # Queue waits and service times are measured where items were answered
    for lane in submitted:
        assert any(s["lanes"][lane]["wait_ms_p50"] is not None for s in snaps), lane
    served = [u for s in snaps for u in s["units"].values() if u["service_ms_p50"] is not None]
    assert served and all(u["service_ms_p90"] >= u["service_ms_p50"] >= 0 for u in served)
