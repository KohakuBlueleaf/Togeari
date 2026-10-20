"""One config of the GIL study, in its own process:
``python -m p8.contention.child --work torch --isolation thread --scale full --out x.json``.

The machine (core from ``TOGEARI_CORE``) starts the engine; a feeder thread keeps the engine
full directly while a chain feeder loads the dispatcher at each load of the scale. Then each
load runs alone, so the dispatcher's own numbers at that load are known. Writes one JSON
object: the config, the core that ran, every phase.
"""

import argparse
import json
import os
import time
import traceback
from pathlib import Path

from s1.common import dispatch, envinfo, phases
from s1.common.feeders import JobFeeder, UnitFeeder

from p8.contention import works
from p8.scales import SCALES, Scale


def chain(machine, scale: Scale, rate: float | None) -> list:
    """The chain feeder at ``rate`` jobs/s (None: unpaced); none for 0."""
    if rate == 0:
        return []
    return [JobFeeder(machine, "chain", _chain_inputs, scale.chain_inflight, rate, name="chain")]


def _chain_inputs(i: int) -> dict:
    return {"x": i}


def run(work: str, isolation: str, scale: Scale) -> dict:
    w = works.build(work, isolation, scale)
    t0 = time.perf_counter()
    machine = dispatch.build(scale.chain_len, work=w.node, libraries=w.libraries, bind=w.bind)
    start_s = time.perf_counter() - t0
    core = getattr(machine, "core", "python")
    probes = {"work": w.probe}
    rows = []
    try:
        # The engine fed directly at every load, then every load alone
        for name, rate in scale.loads:
            feeders = [UnitFeeder(w.unit, w.make, batch=w.batch, name="work")]
            feeders += chain(machine, scale, rate)
            rows.append(_phase(f"work+{name}", scale, feeders, machine, probes))
        for name, rate in scale.loads:
            if rate != 0:
                rows.append(_phase(f"{name} alone", scale, chain(machine, scale, rate), machine))
        spec = w.engine.spec()
    finally:
        machine.stop()
    return {"core": core, "start_s": start_s, "guard": w.guard, "engine": spec, "phases": rows}


def _phase(name: str, scale: Scale, feeders: list, machine, probes=None) -> dict:
    try:
        return phases.run(name, scale, feeders, machine, probes)
    except Exception:  # noqa: BLE001
        return {"phase": name, "error": traceback.format_exc()}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work", required=True)
    p.add_argument("--isolation", required=True)
    p.add_argument("--scale", choices=sorted(SCALES), default="small")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    cfg = {"work": args.work, "isolation": args.isolation, "core": os.environ.get("TOGEARI_CORE")}
    try:
        result, error = run(args.work, args.isolation, SCALES[args.scale]), None
    except Exception:  # noqa: BLE001
        result, error = {}, traceback.format_exc()
        print(error, flush=True)
    doc = {
        "config": cfg,
        "error": error,
        "seconds": time.perf_counter() - t0,
        "env": envinfo.collect(),
        **result,
    }
    out.write_text(json.dumps(doc, indent=1, default=repr), encoding="utf-8")


if __name__ == "__main__":
    main()
