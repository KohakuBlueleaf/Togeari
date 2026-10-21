"""Study P8, speed baselines (spec/11 §11.6) per variant, one benchmark at a time.

Runs, each in its own process with ``TOGEARI_CORE`` and the variant's knobs: the dispatcher
ceiling (5 rounds), the scaling shapes, P6 study A (event levels; its ``examples/mixed`` part
on ``cuda:0``) and P6 study B (knob defaults), at the given P6 scale. Run from the repository
root: ``.venv/Scripts/python.exe scripts/studies/p8/baseline.py --variants python,rust``.
Variants are those of ``p8/variants.py``.

Output under ``runs/p8/baseline/<variant>/``: ``ceiling.json``, ``scaling.json``, ``p6/<scale>/A``
and ``p6/<scale>/B``, ``logs/<bench>.log``, and ``baseline.json`` with each command, exit code,
seconds and the headline numbers.
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
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(STUDIES))

from p8.variants import knobs_arg, pick  # noqa: E402

BENCHES = ("ceiling", "scaling", "p6-A", "p6-B")

# Only cuda:0 (by PCI bus order) is visible to every child
GPU_ENV = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": "0"}


def commands(
    root: Path, variant: dict, p6_scale: str, rounds: int, rnd: int | None = None
) -> dict[str, list[str]]:
    """Each bench's command; with ``rnd``, one round of scaling and P6 into round folders."""
    knobs = knobs_arg(variant)
    p6 = ("--scale", p6_scale, "--out", str(p6_root(root, rnd)), *knobs)
    if rnd is not None:
        p6 += ("--round", str(rnd))
    return {
        "ceiling": [
            str(SCRIPTS / "benches" / "dispatcher_ceiling.py"),
            str(root / "ceiling.json"),
            str(rounds),
            *knobs,
        ],
        "scaling": [str(SCRIPTS / "benches" / "scaling.py"), str(scaling_path(root, rnd)), *knobs],
        "p6-A": [str(SCRIPTS / "studies" / "p6" / "run.py"), "--study", "A", *p6],
        "p6-B": [str(SCRIPTS / "studies" / "p6" / "run.py"), "--study", "B", *p6],
    }


def scaling_path(root: Path, rnd: int | None) -> Path:
    return root / ("scaling.json" if rnd is None else f"scaling-r{rnd}.json")


def p6_root(root: Path, rnd: int | None) -> Path:
    return root / ("p6" if rnd is None else f"p6-r{rnd}")


def outputs(name: str, root: Path, p6_scale: str, rnd: int | None) -> list[Path]:
    """The files a bench writes, removed before it runs so none is read from an earlier run."""
    match name:
        case "ceiling":
            return [root / "ceiling.json"]
        case "scaling":
            return [scaling_path(root, rnd)]
        case "p6-A" | "p6-B":
            return [p6_root(root, rnd) / p6_scale / name[-1] / "results.json"]
    return []


def headline(name: str, root: Path, p6_scale: str) -> dict | None:
    """The numbers the docs quote, read from the bench's own output."""
    match name:
        case "ceiling":
            path = root / "ceiling.json"
            if path.exists():
                summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
                return {shape: s["default"] for shape, s in summary.items()}
        case "scaling":
            path = root / "scaling.json"
            if path.exists():
                rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
                return {f"{r['shape']} {r['n']}": r["us"] for r in rows}
        case "p6-A" | "p6-B":
            path = root / "p6" / p6_scale / name[-1] / "summary.md"
            if path.exists():
                return {"summary": str(path.relative_to(REPO))}
    return None


def run_child(cmd: list[str], env: dict, log: Path, timeout: float | None) -> dict:
    """``cmd`` in its own process with its output in ``log``: exit code ("timeout" past
    ``timeout`` seconds, the child then killed) and seconds."""
    t0 = time.perf_counter()
    try:
        with log.open("w", encoding="utf-8") as f:
            proc = subprocess.run(
                [sys.executable, *cmd],
                stdout=f,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=REPO,
                timeout=timeout,
            )
        code: int | str = proc.returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    return {"exit_code": code, "seconds": time.perf_counter() - t0}


def run_bench(name: str, cmd: list[str], core: str, log: Path, timeout: float | None) -> dict:
    """One bench in its own process on ``core``; its command, exit code, seconds and log."""
    env = {**os.environ, **GPU_ENV, "TOGEARI_CORE": core, "PYTHONUNBUFFERED": "1"}
    return {
        "command": cmd,
        "core": core,
        **run_child(cmd, env, log, timeout),
        "log": str(log.relative_to(REPO)),
    }


def run_variant(
    name: str,
    variant: dict,
    out: Path,
    benches: list[str],
    p6_scale: str,
    rounds: int,
    rnd: int | None = None,
    timeouts: dict | None = None,
    scaling_args: list[str] | None = None,
) -> dict:
    """Every bench of ``benches`` for one variant (one round of scaling and P6 with ``rnd``);
    each bench's outputs are removed first; ``baseline.json`` written after each."""
    root = out / name
    (root / "logs").mkdir(parents=True, exist_ok=True)
    cmds = commands(root, variant, p6_scale, rounds, rnd)
    cmds["scaling"] += scaling_args or []

    manifest_path = root / "baseline.json"
    manifest = {"variant": name, **variant, "benches": {}}
    if manifest_path.exists():
        manifest["benches"] = json.loads(manifest_path.read_text(encoding="utf-8"))["benches"]
    for bench in benches:
        for path in outputs(bench, root, p6_scale, rnd):
            path.unlink(missing_ok=True)
        tag = bench if rnd is None else f"{bench}-r{rnd}"
        log = root / "logs" / f"{tag}.log"
        timeout = (timeouts or {}).get(bench)
        entry = run_bench(bench, cmds[bench], variant["core"], log, timeout)
        entry["headline"] = headline(bench, root, p6_scale) if rnd is None else None
        manifest["benches"][tag] = entry
        manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        print(
            f"{name:<10} {tag:<11} exit {entry['exit_code']}  {entry['seconds']:7.1f}s",
            flush=True,
        )
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--variants", default="python")
    p.add_argument("--only", default="", help="comma-separated benches (default: all)")
    p.add_argument("--p6-scale", default="full", choices=("small", "full"))
    p.add_argument("--rounds", type=int, default=5, help="ceiling rounds")
    p.add_argument("--out", default=str(REPO / "runs" / "p8" / "baseline"))
    args = p.parse_args()

    benches = [n for n in args.only.split(",") if n] or list(BENCHES)
    for name, variant in pick(args.variants).items():
        run_variant(name, variant, Path(args.out), benches, args.p6_scale, args.rounds)


if __name__ == "__main__":
    main()
