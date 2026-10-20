"""One measured run of a machine: throughput, peaks, lanes, units, inline node times."""

import statistics
import time
from typing import Any

from togeari import Machine


class Peaks:
    """Resident jobs and pool bytes at each admission (read on the dispatcher thread)."""

    def __init__(self, machine: Machine):
        self.d = machine.dispatcher
        self.jobs = 0
        self.bytes = 0
        machine.bus.listen(self.seen, kinds=["job.admit"])

    def seen(self, kind: str, fields: dict) -> None:
        self.jobs = max(self.jobs, len(self.d.jobs))
        self.bytes = max(self.bytes, self.d.pool_bytes)


def percentiles(times: list[float]) -> dict:
    """p50 / p90 / p99 / max of ``times`` in microseconds."""
    if not times:
        return {}
    s = sorted(times)

    def at(q: float) -> float:
        return s[min(len(s) - 1, int(q * len(s)))] * 1e6

    return {
        "n": len(s),
        "p50_us": at(0.5),
        "p90_us": at(0.9),
        "p99_us": at(0.99),
        "max_us": s[-1] * 1e6,
    }


def inline_times(machine: Machine) -> dict:
    """Per inline node: the recorded run-time percentiles."""
    return {node: percentiles(list(stat.times)) for node, stat in machine.dispatcher.inline.items()}


def unit_busy(machine: Machine, seconds: float) -> dict:
    """Busy share per unit over the run, from each lane's unit state (machine stopped)."""
    now = time.monotonic()
    busy = {}
    for lane in machine.lanes:
        for st in lane.state.values():
            open_s = now - st.busy_since if st.busy_since is not None else 0.0
            busy[st.unit.name] = (st.busy_total + open_s) / seconds if seconds > 0 else 0.0
    return busy


def run(machine: Machine, source: Any, sink: Any) -> dict:
    """Run ``source`` through ``machine`` once, stop it, and report everything measured."""
    peaks = Peaks(machine)
    try:
        t0 = time.perf_counter()
        summary = machine.run(source, sink)
        seconds = time.perf_counter() - t0
        stats = machine.inspect("stats").result(30)
    finally:
        machine.stop()
    busy = unit_busy(machine, seconds)

    calls = stats["calls"]
    return {
        "seconds": seconds,
        "jobs": summary["jobs"],
        "jobs_ok": summary["jobs_ok"],
        "calls": calls,
        "calls_per_s": calls / seconds,
        "jobs_per_s": summary["jobs"] / seconds,
        "us_per_call": seconds / calls * 1e6 if calls else None,
        "dispatcher_busy": summary["dispatcher_busy"],
        "dispatcher_us_per_call": stats["busy_s"] / calls * 1e6 if calls else None,
        "phase_s": stats["phase_s"],
        "squashed": summary["squashed"],
        "peak_jobs": peaks.jobs,
        "peak_pool_bytes": peaks.bytes,
        "lanes": summary["lanes"],
        "unit_busy": busy,
        "inline": inline_times(machine),
        "events": dict(machine.bus.counts),
    }


def spread(values: list[float]) -> dict:
    """Median, best and worst of one setting's rounds."""
    if not values:
        return {}
    return {
        "median": statistics.median(values),
        "best": max(values),
        "worst": min(values),
        "rounds": len(values),
    }


def batch_mean(sizes: dict) -> float | None:
    """Mean batch size from a lane's ``batch_sizes`` histogram."""
    n = sum(sizes.values())
    return sum(int(k) * v for k, v in sizes.items()) / n if n else None
