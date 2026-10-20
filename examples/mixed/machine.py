"""The mixed machine: one job per station, three kinds of engine behind one program.

- ``lookup`` asks a local HTTP registry (``WebCallEngine``); unknown stations fail
  the call and the program's ``@try`` keeps gain 1.0;
- ``features`` runs pure-Python spectra in worker processes (``PythonCallEngine``);
- ``embed`` runs a small network on ``cuda:0`` (``TorchEngine``), batched by the lane;
- ``classify`` and ``majority`` run inline;
- the ordered ``note`` / ``close`` append to the survey log from worker threads.

Run it with ``python examples/mixed/machine.py``. The run report (with the recorded
jobs) and the survey log go to ``runs/mixed/``; afterwards
``togeari conform runs/mixed/report`` (with ``examples/mixed`` on ``PYTHONPATH``)
replays every job through the reference interpreter, and
``--reexecute features --reexecute classify --reexecute majority`` also runs those
nodes again. ``python examples/mixed/machine.py 12`` runs 12 stations instead of 120. It exits
nonzero when a job fails, does not match the reference interpreter, or the log is out of order.
"""

import math
import random
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import nodes  # noqa: E402
import scorer  # noqa: E402
from service import Registry  # noqa: E402
from spectra import BINS  # noqa: E402

from togeari import Inline, Lane, Machine, Use  # noqa: E402
from togeari.adapters.http import JsonHttp  # noqa: E402
from togeari.adapters.torch import TorchAdapter  # noqa: E402
from togeari.conformance import conform  # noqa: E402
from togeari.engines.python import PythonCallEngine  # noqa: E402
from togeari.engines.torch import TorchEngine, TorchModule  # noqa: E402
from togeari.engines.web import WebCallEngine  # noqa: E402
from togeari.io import Iterable, Memory  # noqa: E402
from togeari.plugins import RunReport  # noqa: E402
from togeari.policies import SizeOrWait  # noqa: E402

OUT = HERE.parents[1] / "runs" / "mixed"
N_STATIONS = 120
SAMPLES = 1024


def station(i: int) -> dict:
    """A station with 4-12 windows of a few noisy tones."""
    rng = random.Random(i)
    tones = [(rng.randint(1, BINS), rng.uniform(0.2, 2.0)) for _ in range(rng.randint(1, 3))]
    windows = []
    for w in range(4 + i % 9):
        phase = rng.uniform(0, 2 * math.pi)
        samples = [
            sum(a * math.sin(2 * math.pi * f * t / SAMPLES + phase) for f, a in tones)
            + rng.gauss(0, 0.3)
            for t in range(SAMPLES)
        ]
        windows.append({"i": w, "samples": samples})
    return {"station": f"st{i:03d}", "windows": windows}


def lookup_body(inputs: dict) -> dict:
    return {"station": inputs["station"]}


def gain_of(reply: dict) -> float:
    return reply["gain"]


def build(
    stations: int = N_STATIONS,
    wait_ms: float = 5,
    report: Path | None = OUT / "report",
    plugins: list | None = None,
    admission: Any = None,
    **knobs: Any,
):
    """The machine, its source and its sink (the CLI's machine-module contract).

    ``stations`` sets the source size, ``wait_ms`` the device lane's batch wait, ``report`` the
    run report folder (None: no report), ``plugins`` more plugins, ``admission`` the admission
    rule; other keywords are knobs (``record=1.0`` unless given).
    """
    registry = Registry()
    web = Lane(
        [WebCallEngine(registry.url, max_inflight=16, health_path="/health", name="registry")],
        name="registry",
    )
    cpu = Lane(
        [PythonCallEngine(workers=8, mode="process", imports=("spectra",), name="spectra")],
        name="cpu",
    )

    # The network on cuda:0, in its own process, under a 2 GB allocator cap.
    gpu = TorchEngine(devices=["cuda:0"], memory_cap_gb=2.0, isolation="process")
    gpu.add_model(
        "scorer",
        TorchModule(
            scorer.make_module(),
            collate=scorer.collate,
            split=scorer.to_rows,
            key=scorer.width,
            max_batch=64,
            example=[[0.0] * BINS] * 4,
            name="scorer",
        ),
    )
    score = Lane([gpu.unit("scorer")], former=SizeOrWait(64, wait_ms=wait_ms), name="score")

    # The survey log is written from worker threads, off the dispatcher
    log = Lane([PythonCallEngine(workers=2, mode="thread", name="log")], name="log")

    machine = Machine(
        program=HERE / "stations.kir",
        entry="survey",
        libraries=[nodes.lib],
        bind={
            "lookup": Use(web, adapter=JsonHttp("/lookup", build=lookup_body, parse=gain_of)),
            "features": Use(cpu),
            "embed": Use(score, adapter=TorchAdapter(arg="feats")),
            "classify": Inline(),
            "majority": Inline(),
            "note": Use(log),
            "close": Use(log),
            "kir.std.*": Inline(),
        },
        plugins=[*([RunReport(report)] if report is not None else []), *(plugins or [])],
        admission=admission,
        **{"record": 1.0, **knobs},
    )
    source = Iterable([station(i) for i in range(stations)], key="station")
    return machine, source, Memory()


def check_log(path: Path, records: dict) -> list[str]:
    """Problems in the survey log: each station's windows in order, then its verdict."""
    lines = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, rest = line.partition(" ")
        lines[name].append(rest)

    problems = []
    for key, rec in records.items():
        want = [f"window {i}: {label}" for i, label in enumerate(rec.outputs["labels"])]
        want.append(f"verdict: {rec.outputs['verdict']}")
        if lines.get(key) != want:
            problems.append(f"{key}: log {lines.get(key)} != {want}")
    return problems


def main(stations: int = N_STATIONS) -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    nodes.LOG["path"] = OUT / "survey.log"

    machine, source, sink = build(stations)
    inputs = {s["station"]: s for s in source.items}
    with machine:
        summary = machine.run(source, sink)
        job_program = machine.code.entries["survey"].region.job

    # A few stations and their verdicts
    records = {rec.key: rec for rec in sink.records}
    for key in sorted(records)[:8]:
        rec = records[key]
        print(f"{key}: {rec.outputs.get('verdict'):<14} {rec.outputs.get('labels')}")
    print(f"... {len(records)} stations, {sum(not r.ok for r in records.values())} failed")

    # Every job against the reference interpreter, and the ordered log
    verdicts = [conform(job_program, rec, inputs[rec.key]) for rec in records.values()]
    print(f"conformance: {sum(v.ok for v in verdicts)}/{len(verdicts)} jobs match the reference")
    problems = check_log(OUT / "survey.log", records)
    print(f"ordered log: {len(problems)} problem(s)")
    for p in problems[:5]:
        print("  ", p)
    failed = [key for key, rec in records.items() if not rec.ok]
    mismatched = [rec.key for rec, v in zip(records.values(), verdicts, strict=True) if not v.ok]

    # Where the time went
    print(f"{summary['jobs']} jobs, {summary['calls']} node calls in {summary['seconds']:.2f}s")
    for name, lane in summary["lanes"].items():
        print(
            f"  lane {name:<9} submitted {lane['submitted']:>5}  batch sizes {lane['batch_sizes']}"
        )
    print(f"report: {OUT / 'report'}")
    if failed or mismatched or problems:
        print(f"failed {failed}, not matching the reference {mismatched}")
        sys.exit(1)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else N_STATIONS)
