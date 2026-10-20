"""The hello machine: translation on a simulated slow service, the rest inline.

Run it with ``python examples/hello/machine.py`` (or ``togeari run examples/hello/machine.py``).
It exits nonzero when a job fails or does not match the reference interpreter.
"""

import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import nodes  # noqa: E402

from togeari import Inline, Lane, Machine, Use  # noqa: E402
from togeari.conformance import conform  # noqa: E402
from togeari.io import Iterable, Memory  # noqa: E402
from togeari.testing import SimCall  # noqa: E402

PEOPLE = [
    {"name": "Ayu", "langs": ["en", "ja", "fr"]},
    {"name": "Bao", "langs": ["zh", "es", "de", "en"]},
    {"name": "Chi", "langs": ["ja"]},
    {"name": "Dev", "langs": ["fr", "xx", "en"]},
    {"name": "Eli", "langs": []},
]

# `togeari units`: sample calls per unit, and calls that must fail
unit_samples = {"translator": [("translate", {"lang": "fr"}), ("translate", {"lang": "de"})]}
unit_check_args = {"translator": {"failing_items": [("translate", {"lang": "xx"})]}}


def build():
    """The machine, its source and its sink (the CLI's machine-module contract)."""
    service = Lane([SimCall(name="translator", latency_ms=50, capacity=4)], name="translate")
    machine = Machine(
        program=HERE / "hello.kir",
        entry="greet",
        libraries=[nodes.lib],
        bind={
            "translate": Use(service),
            "compose": Inline(),
            "kir.std.*": Inline(),
        },
        record=1.0,
    )
    return machine, Iterable(PEOPLE, key="name"), Memory()


def verify(job_program, records: list, inputs: dict) -> list[str]:
    """Jobs that failed or do not match the reference interpreter."""
    problems = []
    for rec in records:
        verdict = conform(job_program, rec, inputs[rec.key])
        if not rec.ok or not verdict.ok:
            problems.append(f"{rec.key}: {rec.failure or verdict.reason}")
    return problems


def main() -> None:
    machine, source, sink = build()
    with machine:
        summary = machine.run(source, sink)
        job_program = machine.code.entries["greet"].region.job

    # Each person's cards
    for rec in sorted(sink.records, key=lambda r: r.key):
        print(f"{rec.key}: {rec.outputs.get('count')} cards {rec.outputs.get('cards')}")

    # Every job checked against the reference interpreter
    problems = verify(job_program, sink.records, {p["name"]: p for p in PEOPLE})
    n = len(sink.records)
    print(f"conformance: {n - len(problems)}/{n} jobs match the reference")
    print(f"{summary['jobs']} jobs, {summary['calls']} node calls in {summary['seconds']:.2f}s")
    for p in problems:
        print("  ", p)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
