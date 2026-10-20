"""Percentiles, CPU clocks and deltas of nested counters."""

import math
import os
import time
from typing import Any

try:
    import psutil
except ImportError:
    psutil = None


def percentiles(xs: list[float]) -> dict:
    """Count, mean, min, p50, p90, p99 and max of ``xs`` (nearest rank); empty gives n=0."""
    if not xs:
        return {"n": 0}
    s = sorted(xs)

    # Nearest-rank percentile
    def rank(q: float) -> float:
        return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]

    return {
        "n": len(s),
        "mean": sum(s) / len(s),
        "min": s[0],
        "p50": rank(0.50),
        "p90": rank(0.90),
        "p99": rank(0.99),
        "max": s[-1],
    }


def scaled(stats: dict, factor: float) -> dict:
    """``percentiles`` output with every value but ``n`` multiplied by ``factor``."""
    return {k: (v if k == "n" else v * factor) for k, v in stats.items()}


def cpu_now() -> dict:
    """Wall clock and this process's CPU seconds (plus its children's, with psutil)."""
    t = os.times()
    snap = {
        "wall": time.perf_counter(),
        "epoch": time.time(),
        "process_cpu": time.process_time(),
        "user": t.user,
        "system": t.system,
        "children_cpu": None,
    }
    if psutil is not None:
        total = 0.0
        for child in psutil.Process().children(recursive=True):
            try:
                c = child.cpu_times()
                total += c.user + c.system
            except psutil.Error:
                continue
        snap["children_cpu"] = total
    return snap


def cpu_delta(a: dict, b: dict) -> dict:
    """CPU seconds between two ``cpu_now`` snapshots, and cores busy on average."""
    wall = b["wall"] - a["wall"]
    out = {
        "wall_s": wall,
        "process_cpu_s": b["process_cpu"] - a["process_cpu"],
        "user_s": b["user"] - a["user"],
        "system_s": b["system"] - a["system"],
        "children_cpu_s": None,
    }
    if a["children_cpu"] is not None and b["children_cpu"] is not None:
        out["children_cpu_s"] = b["children_cpu"] - a["children_cpu"]
    out["process_cores"] = out["process_cpu_s"] / wall if wall > 0 else 0.0
    return out


def delta(a: Any, b: Any) -> Any:
    """``b - a`` through nested dicts of numbers; other values are taken from ``b``."""
    match a, b:
        case dict(), dict():
            return {k: delta(a.get(k, _zero(v)), v) for k, v in b.items()}
        case (int() | float()), (int() | float()) if not isinstance(b, bool):
            return b - a
        case _:
            return b


def _zero(v: Any) -> Any:
    match v:
        case dict():
            return {}
        case int() | float():
            return 0
        case _:
            return None
