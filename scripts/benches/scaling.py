"""Dispatcher cost as the machine grows: time per job with N resident jobs (all admitted at
once, so lane queues and the job pool hold N), and time per item of one wide `each`.

Every size runs ``repeats`` times on a fresh machine; the minimum is kept. Writes one JSON
report and prints a table; ``tests/scaling`` asserts the cost at 4N stays within a constant
factor of the cost at N.
"""

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


def timed(program: str, jobs: list, resident: int) -> float:
    """Seconds of one run of ``jobs`` with up to ``resident`` jobs admitted at once."""
    lane = Lane([SimCall(name="c", latency_ms=0.0, capacity=64)])
    m = Machine(
        program=program,
        entry="main",
        libraries=[lib],
        bind={"inc": Use(lane), "kir.std.*": Inline()},
        admission=Fixed(resident),
        source_buffer=max(resident, 1),
    )
    try:
        sink = Memory()
        t0 = time.perf_counter()
        m.run(Iterable(jobs), sink)
        dt = time.perf_counter() - t0
    finally:
        m.stop()
    assert len(sink.records) == len(jobs) and all(r.ok for r in sink.records)
    return dt


def per_job_us(n: int, repeats: int = 3) -> float:
    """Microseconds per job with ``n`` jobs resident at once."""
    jobs = [{"x": i} for i in range(n)]
    return min(timed(ONE, jobs, n) for _ in range(repeats)) / n * 1e6


def per_item_us(n: int, repeats: int = 3) -> float:
    """Microseconds per item of one `each` over ``n`` items."""
    return min(timed(WIDE, [{"n": n}], 1) for _ in range(repeats)) / n * 1e6


def main(out: str) -> None:
    rows = [{"shape": "resident", "n": n, "us": per_job_us(n)} for n in (500, 2000, 8000)]
    rows += [{"shape": "each", "n": n, "us": per_item_us(n)} for n in (250, 1000, 4000)]
    report = {"python": sys.version, "machine": platform.platform(), "rows": rows}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, indent=2))
    for r in rows:
        unit = "job" if r["shape"] == "resident" else "item"
        print(f"{r['shape']:<9} {r['n']:>6}  {r['us']:8.1f} us/{unit}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "runs/bench/scaling.json")
