"""A child process of the study: one config (or every config) of one measurement, or the
environment.

``python -m s1.child --measure m2 --scale full --config deep/thread/eager --out x.json``
writes a JSON list of config results; ``--env`` writes the environment instead.
A config that raises is recorded with its traceback.
"""

import argparse
import json
import time
import traceback
from pathlib import Path
from types import ModuleType

from s1 import measures
from s1.common import envinfo
from s1.scales import SCALES, Scale


def run_config(module: ModuleType, cfg: dict, scale: Scale) -> dict:
    epoch0, t0 = time.time(), time.perf_counter()
    try:
        result, error = module.run(cfg, scale), None
    except Exception:  # noqa: BLE001
        result, error = {}, traceback.format_exc()
    return {
        "name": cfg["name"],
        "config": cfg,
        "error": error,
        "epoch_start": epoch0,
        "epoch_end": time.time(),
        "seconds": time.perf_counter() - t0,
        **result,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--measure")
    p.add_argument("--scale", choices=sorted(SCALES), default="smoke")
    p.add_argument("--config", help="one config by name; every config when omitted")
    p.add_argument("--env", action="store_true")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.env:
        out.write_text(json.dumps(envinfo.collect(), indent=1, default=repr))
        return

    scale = SCALES[args.scale]
    module = measures.load(args.measure)
    configs = [c for c in module.plan(scale) if args.config in (None, c["name"])]
    if not configs:
        raise SystemExit(f"no config {args.config!r} in {args.measure}")
    results = []
    for cfg in configs:
        print(f"[{args.measure}] {cfg['name']}", flush=True)
        results.append(run_config(module, cfg, scale))
        print(f"[{args.measure}] {cfg['name']} done in {results[-1]['seconds']:.1f}s", flush=True)
        if results[-1]["error"]:
            print(results[-1]["error"], flush=True)
    out.write_text(json.dumps(results, indent=1, default=repr))


if __name__ == "__main__":
    main()
