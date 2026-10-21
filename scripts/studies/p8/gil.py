"""Study P8, GIL contention (spec/11 §11.6): engine throughput against dispatcher load.

Per variant (``p8/variants.py``: a core and its knobs), work (a torch network on ``cuda:0``, a
GIL-holding Python loop) and isolation (``thread``, ``process``): the engine is fed directly
from a feeder thread while chain jobs load the dispatcher at each of the scale's loads, from
idle to its ceiling. Each config runs in its own child process, one at a time, with
``TOGEARI_CORE`` set and only ``cuda:0`` visible. A variant whose core is not built is recorded
as skipped.

Run from the repository root: ``.venv/Scripts/python.exe scripts/studies/p8/gil.py --scale
full --variants python,rust-o0,rust-onone`` (``small`` checks the script; every variant by
default). Output under
``runs/p8/gil/<scale>/``: ``configs/<variant>-<work>-<isolation>.json`` and ``logs/`` per
config, ``gil.json``, ``summary.md``.
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
from p8.variants import ALL, knobs_arg, pick, rotated  # noqa: E402
from togeari.core.construct.core import rust_available  # noqa: E402

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


def run_config(
    name: str, variant: dict, work: str, isolation: str, scale_name: str, root: Path, rnd: int
):
    tag = f"{name}-{work}-{isolation}-r{rnd}"
    out = root / "configs" / f"{tag}.json"
    log = root / "logs" / f"{tag}.log"
    cfg = {
        "variant": name,
        "core": variant["core"],
        "work": work,
        "isolation": isolation,
        "round": rnd,
    }
    if not core_built(variant["core"]):
        return {"config": cfg, "skipped": "no native dispatcher"}
    out.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)

    args = ["--work", work, "--isolation", isolation, "--scale", scale_name, "--out", str(out)]
    args += ["--variant", name, *knobs_arg(variant)]
    t0 = time.perf_counter()
    timeout = SCALES[scale_name].child_timeout_s
    child = {"log": str(log)}
    try:
        with log.open("w", encoding="utf-8") as f:
            proc = subprocess.run(
                [sys.executable, "-m", "p8.contention.child", *args],
                stdout=f,
                stderr=subprocess.STDOUT,
                env=child_env(variant["core"]),
                cwd=REPO,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired:
        # A child past its timeout is a failed config; the study goes on
        child |= {"exit_code": "timeout", "seconds": time.perf_counter() - t0}
        return {"config": cfg, "error": f"timeout after {timeout:.0f}s; see {log}", "child": child}
    seconds = time.perf_counter() - t0
    child |= {"exit_code": proc.returncode, "seconds": seconds}
    if not out.exists():
        error = f"child exit {proc.returncode}, no result; see {log}"
        return {"config": cfg, "error": error, "child": child}
    result = json.loads(out.read_text(encoding="utf-8"))
    result["config"]["round"] = rnd
    result["child"] = child
    return result


def run_study(variants: dict[str, dict], scale_name: str, out: Path) -> tuple[list[dict], Path]:
    """Every config, one child at a time; ``gil.json`` and ``summary.md`` under ``out/<scale>``."""
    scale = SCALES[scale_name]
    root = out / scale.name
    root.mkdir(parents=True, exist_ok=True)
    loads = [name for name, _ in scale.loads]
    t_study = time.perf_counter()

    # Rounds outermost, so drift reaches every variant and config alike; the variant order
    # rotates by one each round
    results = []
    names = list(variants)
    for rnd in range(scale.rounds):
        for name in rotated(names, rnd):
            variant = variants[name]
            for work in scale.works:
                for isolation in scale.isolations:
                    r = run_config(name, variant, work, isolation, scale.name, root, rnd)
                    results.append(r)
                    state = r.get("skipped") or ("FAILED" if r.get("error") else "ok")
                    rates = [report.work_rate(r, x) for x in loads]
                    shown = "  ".join(
                        f"{x}: {v:,.0f}" for x, v in zip(loads, rates) if v is not None
                    )
                    print(
                        f"r{rnd} {name:<10} {work:<6} {isolation:<7} {state:<14} {shown}",
                        flush=True,
                    )

    doc = {
        "title": TITLE,
        "description": __doc__,
        "scale": scale.name,
        "variants": variants,
        "results": results,
    }
    (root / "gil.json").write_text(json.dumps(doc, indent=1, default=repr), encoding="utf-8")
    summary = report.write(root, TITLE, results, loads)
    print(f"study done in {time.perf_counter() - t_study:.1f}s -> {summary}", flush=True)
    return results, summary


def main() -> None:
    p = argparse.ArgumentParser(description=TITLE)
    p.add_argument("--scale", choices=sorted(SCALES), default="small")
    p.add_argument("--variants", default=ALL)
    p.add_argument("--out", default=str(REPO / "runs" / "p8" / "gil"))
    args = p.parse_args()
    run_study(pick(args.variants), args.scale, Path(args.out))


if __name__ == "__main__":
    main()
