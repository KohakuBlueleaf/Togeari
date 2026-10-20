"""Study P8, GIL contention (spec/11 §11.6): engine throughput against dispatcher load.

Per core (``python``, ``rust``), work (a torch network on ``cuda:0``, a GIL-holding Python
loop) and isolation (``thread``, ``process``): the engine is fed directly from a feeder thread
while chain jobs load the dispatcher at each of the scale's loads, from idle to its ceiling.
Each config runs in its own child process, one at a time, with ``TOGEARI_CORE`` set and only
``cuda:0`` visible. A core that is not built is recorded as skipped.

Run from the repository root: ``.venv/Scripts/python.exe scripts/studies/p8/gil.py --scale
full`` (``small`` checks the script). Output under ``runs/p8/gil/<scale>/``:
``configs/<core>-<work>-<isolation>.json`` and ``logs/`` per config, ``gil.json``, ``summary.md``.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STUDIES = Path(__file__).resolve().parents[1]
REPO = STUDIES.parents[1]
sys.path.insert(0, str(STUDIES))

from p8.contention import report  # noqa: E402
from p8.scales import SCALES  # noqa: E402
from togeari.core.construct.core import rust_available  # noqa: E402

CORES = ("python", "rust")
TITLE = "GIL contention: engine throughput against dispatcher load"


def core_built(core: str) -> bool:
    """Whether a machine can run on ``core`` (the Rust extension holds its dispatcher)."""
    return core == "python" or rust_available()


def child_env(core: str) -> dict:
    path = [str(STUDIES), *filter(None, [os.environ.get("PYTHONPATH")])]
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(path),
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "CUDA_VISIBLE_DEVICES": "0",
        "PYTHONUNBUFFERED": "1",
        "TOGEARI_CORE": core,
    }


def run_config(core: str, work: str, isolation: str, scale_name: str, root: Path) -> dict:
    tag = f"{core}-{work}-{isolation}"
    out = root / "configs" / f"{tag}.json"
    log = root / "logs" / f"{tag}.log"
    cfg = {"core": core, "work": work, "isolation": isolation}
    if not core_built(core):
        return {"config": cfg, "skipped": "no native dispatcher"}
    out.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)

    args = ["--work", work, "--isolation", isolation, "--scale", scale_name, "--out", str(out)]
    t0 = time.perf_counter()
    with log.open("w", encoding="utf-8") as f:
        proc = subprocess.run(
            [sys.executable, "-m", "p8.contention.child", *args],
            stdout=f,
            stderr=subprocess.STDOUT,
            env=child_env(core),
            cwd=REPO,
            timeout=SCALES[scale_name].child_timeout_s,
            check=False,
        )
    seconds = time.perf_counter() - t0
    if not out.exists():
        return {"config": cfg, "error": f"child exit {proc.returncode}, no result; see {log}"}
    result = json.loads(out.read_text(encoding="utf-8"))
    result["child"] = {"exit_code": proc.returncode, "seconds": seconds, "log": str(log)}
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=TITLE)
    p.add_argument("--scale", choices=sorted(SCALES), default="small")
    p.add_argument("--cores", default=",".join(CORES))
    p.add_argument("--out", default=str(REPO / "runs" / "p8" / "gil"))
    args = p.parse_args()

    scale = SCALES[args.scale]
    root = Path(args.out) / scale.name
    root.mkdir(parents=True, exist_ok=True)
    loads = [name for name, _ in scale.loads]
    t_study = time.perf_counter()

    # One child per config, one at a time
    results = []
    for core in args.cores.split(","):
        for work in scale.works:
            for isolation in scale.isolations:
                r = run_config(core, work, isolation, scale.name, root)
                results.append(r)
                state = r.get("skipped") or ("FAILED" if r.get("error") else "ok")
                rates = [report.work_rate(r, x) for x in loads]
                shown = "  ".join(f"{x}: {v:,.0f}" for x, v in zip(loads, rates) if v is not None)
                print(f"{core:<6} {work:<6} {isolation:<7} {state:<14} {shown}", flush=True)

    doc = {"title": TITLE, "description": __doc__, "scale": scale.name, "results": results}
    (root / "gil.json").write_text(json.dumps(doc, indent=1, default=repr), encoding="utf-8")
    summary = report.write(root, TITLE, results, loads)
    print(f"study done in {time.perf_counter() - t_study:.1f}s -> {summary}", flush=True)


if __name__ == "__main__":
    main()
