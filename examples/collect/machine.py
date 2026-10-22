"""Nested ordered collections over ordinary worker calls, checked against KIR replay."""

from pathlib import Path

from togeari import Inline, Machine, Use
from togeari.conformance import conform
from togeari.engines.python import PythonCallEngine
from togeari.io import Iterable, Memory

HERE = Path(__file__).parent
INPUTS = [{"rows": [[1, 2], [], [3]]}, {"rows": []}, {"rows": [[-1], [0, 4]]}]


def build():
    worker = PythonCallEngine(name="multiply", workers=2, mode="thread")
    machine = Machine(
        program=HERE / "collect.kir",
        entry="main",
        bind={"kir.std.mul": Use(worker), "kir.std.*": Inline()},
        record=1.0,
        window=4,
    )
    return machine, Iterable(INPUTS), Memory()


def main():
    machine, source, sink = build()
    with machine:
        machine.run(source, sink)
        job = machine.code.entries["main"].region.job
    problems = []
    for record in sorted(sink.records, key=lambda r: r.key):
        inputs = INPUTS[int(record.key)]
        expected = [[2 * value for value in row] for row in inputs["rows"]]
        verdict = conform(job, record, inputs)
        rerun = conform(job, record, inputs, mode="reexecute", pure=lambda _: True)
        if (
            not record.ok
            or record.outputs != {"results": expected}
            or not verdict.ok
            or not rerun.ok
        ):
            problems.append(f"{record.key}: {record.failure or verdict.reason or rerun.reason}")
        print(f"{record.key}: {record.outputs}")
    if len(sink.records) != len(INPUTS):
        problems.append("a job record is missing")
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"conformance: {len(sink.records)}/{len(INPUTS)} jobs match the reference")


if __name__ == "__main__":
    main()
