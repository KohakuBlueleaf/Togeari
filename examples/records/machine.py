"""Immutable records, an ordinary worker engine, explicit codecs and conformance.

Run ``python examples/records/machine.py``; no device or external service is needed.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import record_schemas  # noqa: E402
from kohakunode.records import decode_record, encode_record  # noqa: E402

from togeari import Inline, Machine, Use  # noqa: E402
from togeari.conformance import conform  # noqa: E402
from togeari.engines.python import PythonCallEngine  # noqa: E402
from togeari.io import Iterable, Memory  # noqa: E402

INPUTS = [{"x": 1, "dx": 10}, {"x": -2, "dx": 5}, {"x": 0, "dx": 0}]


def build():
    """The normal machine-module contract; records need no scheduling extension."""
    worker = PythonCallEngine(name="translate", workers=2, mode="thread")
    machine = Machine(
        program=HERE / "records.kir",
        entry="main",
        libraries=[record_schemas.lib],
        bind={"geometry.Point.*": Inline(), "geometry.translate": Use(worker)},
        record=1.0,
        assert_alias=True,
    )
    return machine, Iterable(INPUTS), Memory()


def main():
    machine, source, sink = build()
    with machine:
        machine.run(source, sink)
        job = machine.code.entries["main"].region.job
    schemas = {record_schemas.Point.schema_id: record_schemas.Point}
    problems = []
    for record in sorted(sink.records, key=lambda item: item.key):
        inputs = INPUTS[int(record.key)]
        replayed = conform(job, record, inputs)
        verdict = conform(job, record, inputs, mode="reexecute", pure=lambda _: True)
        if not record.ok or not replayed.ok or not verdict.ok:
            problems.append(f"{record.key}: {record.failure or replayed.reason or verdict.reason}")
            continue
        if record.outputs["original_x"] != inputs["x"]:
            problems.append(f"{record.key}: original record changed")
        if record.outputs["moved_x"] != inputs["x"] + inputs["dx"]:
            problems.append(f"{record.key}: incorrect translated point")

        # A wire boundary uses portable tagged JSON, not Python class reflection.
        wire = json.dumps(encode_record(record.outputs["moved"]), allow_nan=False)
        decoded = decode_record(json.loads(wire), schemas)
        if decoded != record.outputs["moved"]:
            problems.append(f"{record.key}: codec round trip differs")
        print(f"{record.key}: {record.outputs['original_x']} -> {record.outputs['moved_x']}")
    if len(sink.records) != len(INPUTS):
        problems.append("a job record is missing")
    for problem in problems:
        print(problem)
    if problems:
        raise SystemExit(1)
    print(f"conformance: {len(sink.records)}/{len(INPUTS)} jobs match the reference")


if __name__ == "__main__":
    main()
