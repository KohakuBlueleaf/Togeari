"""Measured phases: start the feeders, warm up, measure a window, stop and drain.

Each phase reports its epoch window (for the parent's device samples), the
process CPU, the dispatcher's window numbers when a machine is given, every
feeder's completions in the window, and ``probes`` (named callables returning
counters) before, after and as a delta. A phase that raises is recorded with
its traceback and the next phase runs.
"""

import time
import traceback
from collections.abc import Callable
from typing import Any

from s1.common import dispatch
from s1.common.feeders import JobFeeder
from s1.common.stats import cpu_delta, cpu_now, delta
from s1.scales import Scale

Probes = dict[str, Callable[[], Any]]


def split(phase: str) -> tuple[str | None, str | None]:
    """``"<work>+<load>"`` -> (work, load); a lone part is whichever it names."""
    parts = phase.split("+")
    work = next((p for p in parts if p not in ("ceiling", "paced")), None)
    load = next((p for p in parts if p in ("ceiling", "paced")), None)
    return work, load


def chain_load(machine: Any, scale: Scale, load: str | None) -> list:
    """The chain feeder for ``load`` (none for None)."""
    match load:
        case "ceiling":
            rate = None
        case "paced":
            rate = scale.paced_jobs_per_s
        case _:
            return []
    return [JobFeeder(machine, "chain", _chain_inputs, scale.chain_inflight, rate, name="chain")]


def _chain_inputs(i: int) -> dict:
    return {"x": i}


def _take(machine: Any, probes: Probes) -> dict:
    return {
        "cpu": cpu_now(),
        "dispatcher": dispatch.snapshot(machine) if machine is not None else None,
        "probes": {k: p() for k, p in probes.items()},
    }


def run(
    name: str, scale: Scale, feeders: list, machine: Any = None, probes: Probes | None = None
) -> dict:
    """Run one phase with ``feeders``; every number of the measured window."""
    probes = probes or {}
    drain_s = max(30.0, 10 * scale.phase_s)
    try:
        for f in feeders:
            f.start()
        time.sleep(scale.warmup_s)

        # The measured window
        a = _take(machine, probes)
        time.sleep(scale.phase_s)
        b = _take(machine, probes)
    finally:
        # Stop feeding; what did not finish within the drain limit is reported
        undrained = {f.name: f.stop(drain_s=drain_s) for f in feeders}

    t0, t1 = a["cpu"]["wall"], b["cpu"]["wall"]
    return {
        "phase": name,
        "error": None,
        "epoch_start": a["cpu"]["epoch"],
        "epoch_end": b["cpu"]["epoch"],
        "cpu": cpu_delta(a["cpu"], b["cpu"]),
        "dispatcher": (
            dispatch.window_stats(a["dispatcher"], b["dispatcher"]) if machine is not None else None
        ),
        "feeders": {f.name: f.window(t0, t1) for f in feeders},
        "probes": {
            k: {"before": a["probes"][k], "after": v, "delta": delta(a["probes"][k], v)}
            for k, v in b["probes"].items()
        },
        "undrained": undrained,
    }


def run_all(
    names: tuple[str, ...],
    scale: Scale,
    work: dict[str, Callable[[], list]],
    machine: Any = None,
    probes: Probes | None = None,
    before: Callable[[], None] | None = None,
    label: Callable[[str], str] | None = None,
) -> list[dict]:
    """Every phase in ``names``: ``work[<work part>]()`` gives its feeders, plus the chain load.

    ``label(name)`` is the name each phase is reported under (default: ``name``).
    """
    out = []
    for name in names:
        part, load = split(name)
        shown = label(name) if label is not None else name

        # A failed phase is reported and the next one runs
        try:
            if before is not None:
                before()
            feeders = [
                *(work[part]() if part is not None else []),
                *chain_load(machine, scale, load),
            ]
            out.append(run(shown, scale, feeders, machine, probes))
        except Exception:  # noqa: BLE001
            out.append({"phase": shown, "error": traceback.format_exc()})
    return out
