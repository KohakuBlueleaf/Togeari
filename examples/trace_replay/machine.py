"""A pipeline on simulated engines that replay latency samples per node (latencies.json).

Shows the three lane kinds side by side: ``fetch`` on a stream lane (the engine
queues), ``parse`` on a call lane (four workers), ``score`` on a batch lane
(batches formed by the machine). Run it with ``python examples/trace_replay/machine.py``.
It exits nonzero when a job fails, does not match the reference interpreter, or is lost.
"""

import json
import sys
from pathlib import Path

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.conformance import conform
from togeari.io import Iterable, Memory
from togeari.policies import SizeOrWait, TargetLoad
from togeari.testing import SimBatch, SimCall, SimStream

HERE = Path(__file__).parent
SAMPLES = json.loads((HERE / "latencies.json").read_text())

lib = NodeLibrary("pipeline")


@lib.node(outputs=["doc"])
def fetch(url: str) -> str:
    return " ".join(f"{url}-part{i}" for i in range(len(url) % 5 + 2))


@lib.node(outputs=["chunks"])
def split(doc: str) -> list:
    return doc.split()


@lib.node(outputs=["parsed"])
def parse(chunk: str) -> dict:
    return {"text": chunk, "length": len(chunk)}


@lib.node(outputs=["s"])
def score(parsed: dict) -> float:
    return (parsed["length"] * 37 % 100) / 100


@lib.node(outputs=["best"])
def best_of(scores: list) -> float:
    return max(scores) if scores else 0.0


URLS = [{"url": f"site{i}/page{i * 7 % 13}"} for i in range(60)]


def build():
    """The machine, its source and its sink (the CLI's machine-module contract)."""
    fetcher = Lane([SimStream(name="fetcher", samples_ms=SAMPLES["fetch"])], name="fetch")
    parser = Lane([SimCall(name="parser", samples_ms=SAMPLES["parse"], capacity=4)], name="parse")
    scorer = Lane(
        [SimBatch(name="scorer", samples_ms=SAMPLES["score"], max_batch=32)],
        former=SizeOrWait(32, wait_ms=10),
        name="score",
    )
    machine = Machine(
        program=HERE / "pipeline.kir",
        entry="review",
        libraries=[lib],
        bind={
            "fetch": Use(fetcher),
            "parse": Use(parser),
            "score": Use(scorer),
            "split": Inline(),
            "best_of": Inline(),
            "kir.std.*": Inline(),
        },
        admission=TargetLoad(fetcher, waiting=24),
        record=1.0,
    )
    return machine, Iterable(URLS, key="url"), Memory()


def verify(job_program, records: list) -> list[str]:
    """Jobs that failed, do not match the reference interpreter, or left no record."""
    inputs = {u["url"]: u for u in URLS}
    problems = []
    for rec in records:
        verdict = conform(job_program, rec, inputs[rec.key])
        if not rec.ok or not verdict.ok:
            problems.append(f"{rec.key}: {rec.failure or verdict.reason}")

    # Every source item ends in exactly one record
    seen = {rec.key for rec in records}
    problems.extend(f"{key}: no record" for key in inputs if key not in seen)
    return problems


def main() -> None:
    machine, source, sink = build()
    with machine:
        summary = machine.run(source, sink)
        job_program = machine.code.entries["review"].region.job
    kept = sum(1 for r in sink.records if r.outputs.get("keep"))
    print(f"{summary['jobs']} documents in {summary['seconds']:.2f}s, kept {kept}")

    # What a sequential run would take: every sampled latency, one after another
    one_by_one = sum(
        (sum(SAMPLES["fetch"]) / len(SAMPLES["fetch"]))
        + r.outputs["parts"]
        * (
            sum(SAMPLES["parse"]) / len(SAMPLES["parse"])
            + sum(SAMPLES["score"]) / len(SAMPLES["score"])
        )
        for r in sink.records
    )
    print(f"one job at a time, one call at a time: about {one_by_one / 1000:.1f}s")
    for name, lane in summary["lanes"].items():
        print(
            f"  lane {name:<6} submitted {lane['submitted']:>4}  batch sizes {lane['batch_sizes']}"
        )

    # Every job against the reference interpreter
    problems = verify(job_program, sink.records)
    n = len(URLS)
    print(f"conformance: {n - len(problems)}/{n} jobs match the reference")
    for p in problems:
        print("  ", p)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
