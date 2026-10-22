"""Imported branch/each library on either core, with recorded reference conformance."""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from module_demo_nodes import LIB  # noqa: E402

from togeari import Inline, Machine, SourceItem  # noqa: E402
from togeari.conformance import conform  # noqa: E402
from togeari.io import Iterable, Memory  # noqa: E402

INPUTS = {
    "mixed": {"values": [2, -1, 4]},
    "empty": {"values": []},
    "filtered": {"values": [-3, 0]},
}


def build(core="auto"):
    """CLI machine-module contract: machine, finite source, in-memory sink."""
    machine = Machine(
        program=HERE / "main.kir",
        entry="main",
        libraries=[LIB],
        bind={"module_demo.*": Inline(), "kir.std.*": Inline()},
        core=core,
        record=1.0,
    )
    return machine, Iterable([SourceItem(key, data) for key, data in INPUTS.items()]), Memory()


def run(core: str) -> None:
    machine, source, sink = build(core)
    with machine:
        machine.run(source, sink)
        job = machine.code.entries["main"].region.job
    inputs = INPUTS
    assert len(sink.records) == len(inputs)
    assert {record.key for record in sink.records} == set(inputs)
    for record in sorted(sink.records, key=lambda record: record.key):
        assert record.ok, record.failure
        values = [value for value in inputs[record.key]["values"] if value >= 0]
        expected = {
            "first": [f"primary:{value}" for value in values],
            "second": [f"backup:{value}" for value in values],
        }
        assert record.outputs == expected
        verdict = conform(job, record, inputs[record.key])
        assert verdict.ok, verdict.reason
        verdict = conform(job, record, inputs[record.key], mode="reexecute", pure=lambda _: True)
        assert verdict.ok, verdict.reason
        print(f"{core} {record.key}: {json.dumps(record.outputs, sort_keys=True)}")

    mixed = next(record for record in sink.records if record.key == "mixed")
    origins = [
        origin
        for origin in mixed.stats["record_origins"]
        if origin is not None and origin["definition"] == "decorate"
    ]
    # Two values at each of two call sites: four calls, two static ancestries.
    assert len(origins) == 4
    assert len({origin["node_id"] for origin in origins}) == 1
    assert len({json.dumps(origin["calls"], sort_keys=True) for origin in origins}) == 2
    print(f"{core} conformance: {len(inputs)}/{len(inputs)} replay + reexecute match reference")
    print(f"{core} origins: same definition node, two distinct call-site chains")
    print(json.dumps(origins[0], sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", choices=("python", "rust", "both"), default="both")
    args = parser.parse_args()
    for core in ("python", "rust") if args.core == "both" else (args.core,):
        run(core)


if __name__ == "__main__":
    main()
