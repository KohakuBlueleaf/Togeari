"""M4: Python work in ``PythonCallEngine``, thread mode vs process mode (pickle and shm).

Functions (``workloads/pyfuncs``): ``spin`` holds the GIL, ``digest`` releases
it, ``checksum`` takes an array input. Each runs saturated directly and
through the machine, alone and next to the chain load. Reports items/s, CPU
per item, and the dispatcher's calls/s and busy fraction while it runs; the
function's own time on one thread here is the reference.
"""

import time

import numpy as np

from s1.common import dispatch, phases
from s1.common.feeders import JobFeeder, UnitFeeder
from s1.common.stats import percentiles, scaled
from s1.scales import Scale
from s1.workloads import pyfuncs
from togeari import Use
from togeari.core.contracts.adapter import PythonAdapter
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.python import PythonCallEngine

NAME = "m4"
TITLE = "Python work: thread vs process mode"

SOLO_CALLS = 20
BLOBS = 8


def plan(scale: Scale) -> list[dict]:
    return [
        {"name": f"{fn}/{mode}", "fn": fn, "mode": mode}
        for fn in scale.m4_fns
        for mode in scale.m4_modes
    ]


class Inputs:
    """Inputs of one function, by index."""

    def __init__(self, fn: str, scale: Scale):
        self.fn = fn
        self.scale = scale
        rng = np.random.default_rng(0)
        self.blobs = [rng.integers(0, 256, scale.blob_bytes, dtype=np.uint8) for _ in range(BLOBS)]

    def inputs(self, i: int) -> dict:
        match self.fn:
            case "spin":
                return {"x": i, "n": self.scale.spin_n}
            case "digest":
                return {"x": i, "n": self.scale.hash_bytes}
            case "checksum":
                return {"x": i, "blob": self.blobs[i % BLOBS]}
            case _:
                raise ValueError(f"function {self.fn!r}")


def engine_for(mode: str, scale: Scale) -> PythonCallEngine:
    """``thread`` or ``process/<transport>``."""
    kind, _, transport = mode.partition("/")
    return PythonCallEngine(
        workers=scale.m4_workers,
        mode=kind,
        transport=transport or "pickle",
        imports=(pyfuncs.__name__,),
        name="py",
    )


def solo(fn: object, make: object) -> dict:
    """The function's own time on this thread, in ms."""
    times = []
    for i in range(SOLO_CALLS):
        args = make(i)
        t0 = time.perf_counter()
        fn(**args)
        times.append(time.perf_counter() - t0)
    return scaled(percentiles(times), 1000.0)


def per_item(rows: list[dict]) -> None:
    """Add process CPU per completed work item (µs) to each phase without chain load."""
    for row in rows:
        feeder = row.get("feeders", {}).get("work")
        if feeder is None or row.get("cpu") is None or "chain" in row["feeders"]:
            continue
        ok = feeder["items_ok"]
        row["cpu_us_per_item"] = row["cpu"]["process_cpu_s"] / ok * 1e6 if ok else None


def run(cfg: dict, scale: Scale) -> dict:
    fn = getattr(pyfuncs, cfg["fn"])
    names = pyfuncs.INPUTS[cfg["fn"]]
    data = Inputs(cfg["fn"], scale)
    engine = engine_for(cfg["mode"], scale)
    adapter = PythonAdapter(fn)

    # The machine starts the engine
    t0 = time.perf_counter()
    machine = dispatch.build(
        scale.chain_len,
        work=(cfg["fn"], names),
        libraries=(pyfuncs.lib,),
        bind={cfg["fn"]: Use(engine)},
    )
    start_s = time.perf_counter() - t0

    def item(i: int) -> WorkItem:
        return WorkItem(cfg["fn"], data.inputs(i), adapter, "python")

    work = {
        "direct": lambda: [UnitFeeder(engine, item, name="work")],
        "lane": lambda: [
            JobFeeder(machine, "work", data.inputs, scale.m4_workers * 2, name="work")
        ],
    }
    try:
        rows = phases.run_all(
            scale.m4_phases, scale, work, machine, probes={"engine": lambda: engine.load().extra}
        )
        spec = engine.spec()
    finally:
        machine.stop()
    per_item(rows)
    return {
        "start_s": start_s,
        "solo_ms": solo(fn, data.inputs),
        "engine": spec,
        "phases": rows,
    }
