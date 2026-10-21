"""Study P6: events cost (A), knob defaults (B), device batches (C).

Run from the repository root, one study at a time (GPU studies never run in parallel):

    .venv/Scripts/python.exe scripts/studies/p6/run.py --study A --scale small
    .venv/Scripts/python.exe scripts/studies/p6/run.py --study all --scale full

``--match TEXT`` keeps only the configurations whose name contains ``TEXT``; ``--round R`` only
those of round ``R`` (a caller interleaving variants runs one round per process). ``--knobs JSON``
are set on every machine a study builds, under the knobs the study itself varies; each
configuration records them (``base_knobs``) and each result the core that ran.

Output under ``runs/p6/studies/<scale>/<study>/``: ``configs/<name>.json`` per configuration
and round, ``results.json`` with every result, ``summary.md``, and the run reports and survey
logs the configurations wrote. One line is printed per configuration as it finishes.
"""

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

STUDIES = Path(__file__).resolve().parents[1]
REPO = STUDIES.parents[1]
sys.path.insert(0, str(STUDIES))

# Only cuda:0 is visible to this process and the engines' children
os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")

from p6.common.report import write_config  # noqa: E402
from p6.scales import SCALES  # noqa: E402
from p6.studies import STUDIES as MODULES  # noqa: E402


def run_study(
    key: str, scale_name: str, out: Path, match: str, knobs: dict, only_round: int | None = None
) -> None:
    module = MODULES[key]
    scale = SCALES[scale_name]
    root = out / scale.name / key
    root.mkdir(parents=True, exist_ok=True)
    (root / "results.json").unlink(missing_ok=True)
    cfgs = [{**c, "base_knobs": knobs} for c in module.plan(scale) if match in c["name"]]
    if only_round is not None:
        cfgs = [c for c in cfgs if c["round"] == only_round]
    print(f"study {key}: {module.TITLE} ({len(cfgs)} runs, scale {scale.name})", flush=True)
    t0 = time.perf_counter()

    # Every configuration in plan order; a failure is recorded and the study goes on
    results = []
    for cfg in cfgs:
        t = time.perf_counter()
        try:
            res = module.run(cfg, scale, root)
            entry = {"config": cfg, "result": res}
            text = f"ok {res['jobs_ok']}/{res['jobs']}  {module.line(cfg, res)}"
        except Exception as exc:  # noqa: BLE001
            entry = {
                "config": cfg,
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
            text = f"FAILED {entry['error']}"
        entry["wall_s"] = time.perf_counter() - t
        results.append(entry)
        write_config(root, f"{cfg['name']} r{cfg['round']}", entry)
        print(f"  {cfg['name']:<36} r{cfg['round']}  {text}", flush=True)

    (root / "results.json").write_text(
        json.dumps(
            {
                "study": key,
                "title": module.TITLE,
                "scale": scale.name,
                "description": module.__doc__,
                "results": results,
            },
            indent=1,
            default=repr,
        ),
        encoding="utf-8",
    )
    summary = module.summarize(results, root)
    print(f"study {key} done in {time.perf_counter() - t0:.1f}s -> {summary}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Study P6: events cost, knob defaults, batches")
    p.add_argument("--study", choices=[*MODULES, "all"], default="all")
    p.add_argument("--scale", choices=sorted(SCALES), default="small")
    p.add_argument("--out", default=str(REPO / "runs" / "p6" / "studies"))
    p.add_argument("--match", default="", help="run only configurations whose name has this")
    p.add_argument("--knobs", default="{}", help="JSON knobs for every machine")
    p.add_argument("--round", type=int, default=None, help="run only this round's configs")
    args = p.parse_args()

    keys = list(MODULES) if args.study == "all" else [args.study]
    for key in keys:
        run_study(key, args.scale, Path(args.out), args.match, json.loads(args.knobs), args.round)


if __name__ == "__main__":
    main()
