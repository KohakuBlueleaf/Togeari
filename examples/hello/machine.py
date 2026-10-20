"""The hello machine: translation on a simulated slow service, the rest inline.

Run it with ``python examples/hello/machine.py`` (or ``togeari run examples/hello/machine.py``).
"""

import sys
from pathlib import Path

from togeari import Inline, Lane, Machine, Use
from togeari.conformance import conform
from togeari.io import Iterable, Memory
from togeari.testing import SimCall

sys.path.insert(0, str(Path(__file__).parent))

import nodes

HERE = Path(__file__).parent
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


def main() -> None:
    machine, source, sink = build()
    with machine:
        summary = machine.run(source, sink)

        # Each person's cards
        for rec in sorted(sink.records, key=lambda r: r.key):
            print(f"{rec.key}: {rec.outputs.get('count')} cards {rec.outputs.get('cards')}")

        # Every job checked against the reference interpreter
        job_program = machine.code.entries["greet"].region.job
        people = {p["name"]: p for p in PEOPLE}
        verdicts = [conform(job_program, rec, people[rec.key]) for rec in sink.records]
        print(
            f"conformance: {sum(v.ok for v in verdicts)}/{len(verdicts)} jobs match the reference"
        )
        print(f"{summary['jobs']} jobs, {summary['calls']} node calls in {summary['seconds']:.2f}s")


if __name__ == "__main__":
    main()
