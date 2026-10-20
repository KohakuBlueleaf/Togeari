"""The parallel machine: one job, written sequentially, whose independent work runs at once.

The job stays one job. Its strands (the `parallel` arms, the code after a branch
that is still being decided, one strand per `each` item) fetch ahead, and every
ready call is issued to the lane, which keeps all eight workers busy.

Run it with ``python examples/parallel/machine.py``. Part 1 runs one city and
draws when each call ran; part 2 runs four cities together. It exits nonzero when a job
fails or does not match the reference interpreter.
"""

import sys
import time
from pathlib import Path

from togeari import Inline, Lane, Machine, Use
from togeari.conformance import conform
from togeari.engines.python import PythonCallEngine
from togeari.io import Iterable, Memory

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import nodes  # noqa: E402

DISTRICTS = ["north", "harbor", "old town", "west", "station"]
CITIES = [
    {"city": "Kyoto", "districts": DISTRICTS},
    {"city": "Lisbon", "districts": DISTRICTS[:3]},
    {"city": "Quito", "districts": DISTRICTS[1:]},
    {"city": "Oslo", "districts": DISTRICTS[:2]},
]
SCALE_S = 0.02


def build():
    """The machine, its source and its sink (the CLI's machine-module contract)."""
    io = Lane([PythonCallEngine(workers=8, mode="thread", name="io")], name="io")
    machine = Machine(
        program=HERE / "report.kir",
        entry="report",
        libraries=[nodes.lib],
        bind={
            "weather": Use(io),
            "events": Use(io),
            "traffic": Use(io),
            "alert": Use(io),
            "survey": Use(io),
            "is_stormy": Inline(),
            "compose": Inline(),
            "kir.std.*": Inline(),
        },
        record=1.0,
    )
    return machine, Iterable(CITIES, key="city"), Memory()


def draw(t0: float) -> None:
    """One bar per call: where it started and how long it ran, from the run's start."""
    calls = sorted(nodes.TIMELINE, key=lambda r: r[2])
    end = max(stop for _, _, _, stop in calls)
    width = int((end - t0) / SCALE_S) + 1
    for label, node, start, stop in calls:
        a, b = int((start - t0) / SCALE_S), max(1, int((stop - start) / SCALE_S))
        bar = " " * a + "#" * b
        print(f"  {label:<7} {node:<17} |{bar:<{width}}| {start - t0:5.2f}s")


def verify(job_program, records: list, cities: list) -> list[str]:
    """Jobs that failed or do not match the reference interpreter."""
    by_city = {c["city"]: c for c in cities}
    problems = []
    for rec in records:
        verdict = conform(job_program, rec, by_city[rec.key])
        if not rec.ok or not verdict.ok:
            problems.append(f"{rec.key}: {rec.failure or verdict.reason}")
    return problems


def run(cities: list, problems: list) -> tuple[list, float, float, float]:
    """Run ``cities``, adding to ``problems``; returns the records, the run's start, its
    elapsed time and the summed call time."""
    machine, _, sink = build()
    nodes.TIMELINE.clear()
    with machine:
        # The clock starts once the machine and its workers are up
        t0 = time.time()
        machine.run(Iterable(cities, key="city"), sink)
        elapsed = time.time() - t0
        job_program = machine.code.entries["report"].region.job

    # Every job against the reference interpreter
    found = verify(job_program, sink.records, cities)
    n = len(sink.records)
    print(f"  conformance: {n - len(found)}/{n} jobs match the reference")
    problems.extend(found)
    calls = sum(stop - start for _, _, start, stop in nodes.TIMELINE)
    return sink.records, t0, elapsed, calls


def main() -> None:
    problems: list[str] = []

    # Part 1: one job
    print("part 1: one city, one job")
    records, t0, elapsed, calls = run(CITIES[:1], problems)
    draw(t0)
    rec = records[0]
    print(f"  summary: {rec.outputs['summary']}")
    print(f"  strands at once (peak): {rec.stats.get('strands_peak')}")
    print(f"  calls in flight at once (peak): {rec.stats.get('inflight_peak')}")
    print("  (a strand ends once it has fetched its code; its calls stay in flight after it)")
    print(f"  one job: {elapsed:.2f}s; the same calls one after another: {calls:.2f}s")

    # Part 2: four jobs together
    print("\npart 2: four cities, four jobs at once")
    records, _, elapsed, calls = run(CITIES, problems)
    for rec in sorted(records, key=lambda r: r.key):
        print(f"  {rec.key:<7} {rec.outputs['summary']}")
    print(f"  four jobs: {elapsed:.2f}s; the same calls one after another: {calls:.2f}s")
    for p in problems:
        print("  ", p)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
