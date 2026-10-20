"""Study P8, speed baselines (spec/11 §11.6) on one core, one benchmark at a time.

Runs, each in its own process with ``TOGEARI_CORE`` set: the dispatcher ceiling (5 rounds),
the scaling shapes, P6 study A (event levels; its ``examples/mixed`` part on ``cuda:0``) and
P6 study B (knob defaults), all at full scale. Run from the repository root:
``.venv/Scripts/python.exe scripts/studies/p8/baseline.py --core python``.

Output under ``runs/p8/baseline/<core>/``: ``ceiling.json``, ``scaling.json``, ``p6/full/A``
and ``p6/full/B``, ``logs/<bench>.log``, and ``baseline.json`` with each command, exit code,
seconds and the headline numbers.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"


def commands(root: Path) -> dict[str, list[str]]:
    return {
        "ceiling": [
            str(SCRIPTS / "benches" / "dispatcher_ceiling.py"),
            str(root / "ceiling.json"),
            "5",
        ],
        "scaling": [str(SCRIPTS / "benches" / "scaling.py"), str(root / "scaling.json")],
        "p6-A": [
            str(SCRIPTS / "studies" / "p6" / "run.py"),
            *("--study", "A", "--scale", "full", "--out", str(root / "p6")),
        ],
        "p6-B": [
            str(SCRIPTS / "studies" / "p6" / "run.py"),
            *("--study", "B", "--scale", "full", "--out", str(root / "p6")),
        ],
    }


def headline(name: str, root: Path) -> dict | None:
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
            path = root / "p6" / "full" / name[-1] / "summary.md"
            if path.exists():
                return {"summary": str(path.relative_to(REPO))}
    return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--core", choices=("python", "rust"), default="python")
    p.add_argument("--only", default="", help="comma-separated benches (default: all)")
    p.add_argument("--out", default=str(REPO / "runs" / "p8" / "baseline"))
    args = p.parse_args()

    root = Path(args.out) / args.core
    (root / "logs").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TOGEARI_CORE": args.core, "PYTHONUNBUFFERED": "1"}
    cmds = commands(root)
    names = [n for n in args.only.split(",") if n] or list(cmds)

    # One bench at a time; each result is written as soon as it ends
    manifest_path = root / "baseline.json"
    manifest = {"core": args.core, "benches": {}}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name in names:
        log = root / "logs" / f"{name}.log"
        t0 = time.perf_counter()
        with log.open("w", encoding="utf-8") as f:
            proc = subprocess.run(
                [sys.executable, *cmds[name]], stdout=f, stderr=subprocess.STDOUT, env=env, cwd=REPO
            )
        seconds = time.perf_counter() - t0
        manifest["benches"][name] = {
            "command": cmds[name],
            "exit_code": proc.returncode,
            "seconds": seconds,
            "log": str(log.relative_to(REPO)),
            "headline": headline(name, root),
        }
        manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        print(f"{name:<8} exit {proc.returncode}  {seconds:7.1f}s", flush=True)


if __name__ == "__main__":
    main()
