"""One lane, three units of different speed; the fastest goes down halfway and comes back.

Run it with ``python examples/multi_unit/machine.py``. It exits nonzero when a job fails, sums
wrongly, or does not match the reference interpreter.
"""

import sys
from pathlib import Path

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Retry, Use
from togeari.conformance import conform
from togeari.io import Iterable, Memory
from togeari.policies import Throughput
from togeari.testing import SimCall

HERE = Path(__file__).parent

lib = NodeLibrary("multi")


@lib.node(outputs=["sq"])
def square(n: int) -> int:
    return n * n


JOBS = [{"numbers": list(range(k, k + 12))} for k in range(40)]


def build():
    """The machine, its source and its sink (the CLI's machine-module contract)."""
    units = [
        SimCall(name="slow", latency_ms=20, capacity=4, speed=1.0),
        SimCall(name="medium", latency_ms=20, capacity=4, speed=2.0),
        SimCall(name="fast", latency_ms=20, capacity=4, speed=4.0, outages=[(0.3, 0.8)]),
    ]
    work = Lane(units, select=Throughput(), retry=Retry(max=5), name="work")
    machine = Machine(
        program=HERE / "multi_unit.kir",
        entry="squares",
        libraries=[lib],
        bind={"square": Use(work), "kir.std.*": Inline()},
        record=1.0,
    )
    return machine, Iterable(JOBS), Memory()


def verify(job_program, records: list) -> list[str]:
    """Jobs that failed, sum wrongly, or do not match the reference interpreter."""
    problems = []
    for rec in records:
        job = JOBS[int(rec.key)]
        verdict = conform(job_program, rec, job)
        expected = sum(n * n for n in job["numbers"])
        if not rec.ok or not verdict.ok:
            problems.append(f"{rec.key}: {rec.failure or verdict.reason}")
        elif rec.outputs.get("total") != expected:
            problems.append(f"{rec.key}: total {rec.outputs.get('total')} != {expected}")
    return problems


def main() -> None:
    machine, source, sink = build()
    with machine:
        summary = machine.run(source, sink)
        lanes = machine.inspect("lanes").result()
        job_program = machine.code.entries["squares"].region.job

    # Every job's sum, checked against a plain Python sum and the reference interpreter
    problems = verify(job_program, sink.records)
    wrong = [p.split(":")[0] for p in problems]
    n = len(sink.records)
    print(f"{summary['jobs']} jobs in {summary['seconds']:.2f}s, wrong results: {wrong or 'none'}")
    print(f"conformance: {n - len(problems)}/{n} jobs match the reference")

    # How the lane spread the work
    lane = lanes[0]
    stats = lane["stats"]
    print(
        f"lane {lane['name']}: retries {stats['retries']}, units down {stats['failovers']},"
        f" back up {stats.get('recoveries', 0)}"
    )
    for u in lane["units"]:
        print(
            f"  unit {u['name']:<7} state {u['state']:<5} items {u['done']:>4}"
            f"  errors {u['errors']}"
        )
    for p in problems:
        print("  ", p)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
