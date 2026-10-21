"""Dispatcher cost as the machine grows: time per job with N resident jobs (all admitted at
once, so lane queues and the job pool hold N), and time per item of one wide `each`.

Every size runs ``warmup`` discarded runs, then ``repeats`` measured ones, each on a fresh
machine; the minimum is kept. Each measured run also records the dispatcher's cycles, phase
times and (Rust core) ``native_stats``. Writes one JSON report and prints a table;
``tests/scaling`` asserts the cost at 4N stays within a constant factor of the cost at N.

Usage: ``python scaling.py [out.json] [--knobs JSON] [--warmup 1] [--repeats 3]``; ``--knobs``
go to every machine.
"""

import argparse
import json
import platform
import sys
import time
from pathlib import Path

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.io import Iterable, Memory
from togeari.policies import Fixed
from togeari.testing import SimCall

lib = NodeLibrary("scaling")


@lib.node()
def inc(x: int) -> int:
    return x + 1


ONE = "@def (x: int)main(y):\n    (x)inc(y)\n"
WIDE = """
@def (n: int)main(total):
    (n)range(items)
    total = 0
    (items)each(i, `one`)
    one:
        (i)inc(j)
        (total, j)add(total)
"""


KNOBS: dict = {}
CORES: set[str] = set()


SIZES = {"resident": (500, 2000, 8000), "each": (250, 1000, 4000)}


def timed(program: str, jobs: list, resident: int) -> dict:
    """One run of ``jobs`` with up to ``resident`` jobs admitted at once: its seconds, the
    dispatcher's cycles and phase times, and the Rust core's ``native_stats``."""
    lane = Lane([SimCall(name="c", latency_ms=0.0, capacity=64)])
    m = Machine(
        program=program,
        entry="main",
        libraries=[lib],
        bind={"inc": Use(lane), "kir.std.*": Inline()},
        **{**KNOBS, "admission": Fixed(resident), "source_buffer": max(resident, 1)},
    )
    CORES.add(m.core)
    try:
        sink = Memory()
        t0 = time.perf_counter()
        m.run(Iterable(jobs), sink)
        dt = time.perf_counter() - t0
        stats = m.inspect("stats").result(30)
        native = m.dispatcher.native_stats() if m.core == "rust" else None
    finally:
        m.stop()
    assert len(sink.records) == len(jobs) and all(r.ok for r in sink.records)
    return {
        "seconds": dt,
        "cycles": stats["cycles"],
        "busy_s": stats["busy_s"],
        "phase_s": stats["phase_s"],
        "native_stats": native,
    }


def runs(shape: str, n: int, repeats: int = 3, warmup: int = 0) -> list[dict]:
    """``repeats`` measured runs of ``shape`` at ``n`` after ``warmup`` discarded ones, each with
    its microseconds per job (resident) or per item (each) as ``us``."""
    match shape:
        case "resident":
            args = (ONE, [{"x": i} for i in range(n)], n)
        case "each":
            args = (WIDE, [{"n": n}], 1)
    for _ in range(warmup):
        timed(*args)
    out = []
    for _ in range(repeats):
        r = timed(*args)
        out.append({"us": r["seconds"] / n * 1e6, **r})
    return out


def per_job_us(n: int, repeats: int = 3) -> float:
    return min(r["us"] for r in runs("resident", n, repeats))


def per_item_us(n: int, repeats: int = 3) -> float:
    return min(r["us"] for r in runs("each", n, repeats))


def main(out: str, repeats: int, warmup: int) -> None:
    # ``us`` is the best repeat; ``us_runs`` every measured repeat, for the spread
    rows = []
    for shape, sizes in SIZES.items():
        for n in sizes:
            measured = runs(shape, n, repeats, warmup)
            rows.append(
                {
                    "shape": shape,
                    "n": n,
                    "us_runs": [r["us"] for r in measured],
                    "us": min(r["us"] for r in measured),
                    "warmup": warmup,
                    "runs": measured,
                }
            )
    report = {
        "python": sys.version,
        "machine": platform.platform(),
        "core": sorted(CORES),
        "knobs": KNOBS,
        "rows": rows,
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, indent=2))
    for r in rows:
        unit = "job" if r["shape"] == "resident" else "item"
        print(f"{r['shape']:<9} {r['n']:>6}  {r['us']:8.1f} us/{unit}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Dispatcher cost as the machine grows")
    p.add_argument("out", nargs="?", default="runs/bench/scaling.json")
    p.add_argument("--knobs", default="{}", help="JSON knobs for every machine")
    p.add_argument("--repeats", type=int, default=3, help="measured runs per size")
    p.add_argument("--warmup", type=int, default=1, help="discarded runs per size first")
    args = p.parse_args()
    KNOBS.update(json.loads(args.knobs))
    main(args.out, args.repeats, args.warmup)
