"""Study S1 (engines/05): M1..M5 in sequence, everything written under ``runs/s1/<scale>/``.

Run from the repository root:

    .venv/Scripts/python.exe scripts/studies/s1/run.py --scale smoke
    .venv/Scripts/python.exe scripts/studies/s1/run.py --scale full

Every config runs once, in a child process per config or per measurement (the
scale's ``isolate``, or ``--isolate``), one child at a time, with only ``cuda:0``
visible. While a child runs, this process samples the device with ``nvidia-smi``.
Output:

- ``env.json``: interpreter, platform, CPU, library versions, the device;
- ``m1.json`` .. ``m5.json``: every config's result, with the device samples
  and per-phase device numbers merged in;
- ``raw/<measure>/<config>.json`` and ``logs/<measure>/<config>.log``: each
  child's own output;
- ``summary.md``: tables generated from the JSON files.
"""

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

STUDIES = Path(__file__).resolve().parents[1]
REPO = STUDIES.parents[1]
sys.path.insert(0, str(STUDIES))

from s1 import measures  # noqa: E402
from s1.common import nvsmi, report  # noqa: E402
from s1.scales import SCALES, Scale  # noqa: E402


def child_env() -> dict:
    """The children's environment: this package importable, only ``cuda:0`` visible."""
    path = [str(STUDIES), *filter(None, [os.environ.get("PYTHONPATH")])]
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(path),
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "CUDA_VISIBLE_DEVICES": "0",
        "PYTHONUNBUFFERED": "1",
    }


def kill_tree(proc: subprocess.Popen) -> None:
    """End a child and every process it started."""
    match os.name:
        case "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        case _:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(30)


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def launch(args: list[str], log: Path, timeout_s: float) -> tuple[int | None, float, bool]:
    """Run ``python -m s1.child <args>``; (exit code, seconds, timed out)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    with log.open("w", encoding="utf-8") as f:
        proc = subprocess.Popen(
            [sys.executable, "-m", "s1.child", *args],
            stdout=f,
            stderr=subprocess.STDOUT,
            env=child_env(),
            cwd=REPO,
            start_new_session=os.name != "nt",
        )
        try:
            code = proc.wait(timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            code, timed_out = proc.returncode, True
    return code, time.perf_counter() - t0, timed_out


def tail(log: Path, lines: int = 40) -> str:
    if not log.exists():
        return ""
    return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def run_child(measure: str, cfgs: list[dict], one: bool, scale: Scale, root: Path) -> list[dict]:
    """One child for ``cfgs`` (a single config when ``one``); results with device samples."""
    tag = slug(cfgs[0]["name"]) if one else "all"
    out = root / "raw" / measure / f"{tag}.json"
    log = root / "logs" / measure / f"{tag}.log"
    args = ["--measure", measure, "--scale", scale.name, "--out", str(out)]
    if one:
        args += ["--config", cfgs[0]["name"]]
    if out.exists():
        out.unlink()

    # Device baseline, then samples for the child's whole life
    base = nvsmi.sample()
    sampler = nvsmi.Sampler(scale.gpu_sample_s).start()
    code, seconds, timed_out = launch(args, log, scale.child_timeout_s * len(cfgs))
    samples = sampler.stop()

    child = {
        "exit_code": code,
        "timed_out": timed_out,
        "seconds": seconds,
        "log": str(log.relative_to(root)),
        "raw": str(out.relative_to(root)),
    }
    if out.exists():
        results = json.loads(out.read_text(encoding="utf-8"))
    else:
        error = f"child exit {code}, timed out {timed_out}, no result file\n{tail(log)}"
        results = [{"name": c["name"], "config": c, "error": error} for c in cfgs]

    base_mb = base["used_mb"] if base else None
    for r in results:
        r["child"] = child
        r["gpu"] = {"base": base, "samples": samples}
        if r.get("epoch_start") is not None:
            r["gpu"]["config"] = nvsmi.window(samples, r["epoch_start"], r["epoch_end"], base_mb)
        for ph in r.get("phases", []):
            if ph.get("epoch_start") is not None:
                ph["gpu"] = nvsmi.window(samples, ph["epoch_start"], ph["epoch_end"], base_mb)
    return results


def status(r: dict) -> None:
    """One line per config: seconds and failures."""
    bad = sum(1 for ph in r.get("phases", []) if ph.get("error"))
    bad += sum(1 for s in r.get("sizes", []) if s.get("error"))
    text = "FAILED" if r.get("error") else (f"{bad} part(s) failed" if bad else "ok")
    print(f"  {r['name']:<32} {r.get('seconds') or 0.0:7.1f}s  {text}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Study S1: threads, processes and the GIL")
    p.add_argument("--scale", choices=sorted(SCALES), default="smoke")
    p.add_argument("--out", default=str(REPO / "runs" / "s1"))
    p.add_argument("--only", default="", help="comma-separated measures (default: all)")
    p.add_argument(
        "--isolate", choices=("config", "measure"), help="child per config or per measure"
    )
    args = p.parse_args()

    scale = SCALES[args.scale]
    root = Path(args.out) / scale.name
    root.mkdir(parents=True, exist_ok=True)
    names = [n for n in args.only.split(",") if n] or list(measures.MEASURES)
    t_study = time.perf_counter()

    # Environment, from a child (this process never touches CUDA)
    env_out = root / "env.json"
    launch(["--env", "--out", str(env_out)], root / "logs" / "env.log", 120.0)
    env = json.loads(env_out.read_text(encoding="utf-8")) if env_out.exists() else {}
    isolate = args.isolate or scale.isolate
    env["scale"] = asdict(scale)
    env["isolate"] = isolate
    env_out.write_text(json.dumps(env, indent=1, default=repr), encoding="utf-8")

    for name in names:
        module = measures.load(name)
        configs = module.plan(scale)
        print(f"{name}: {module.TITLE} ({len(configs)} configs)", flush=True)
        t0 = time.perf_counter()

        # A measurement's children write afresh
        for part in ("raw", "logs"):
            shutil.rmtree(root / part / name, ignore_errors=True)
        results: list[dict] = []
        match isolate:
            case "config":
                for cfg in configs:
                    results += run_child(name, [cfg], True, scale, root)
                    status(results[-1])
            case "measure":
                results = run_child(name, configs, False, scale, root)
                for r in results:
                    status(r)
        doc = {
            "measure": name,
            "title": module.TITLE,
            "description": module.__doc__,
            "scale": scale.name,
            "seconds": time.perf_counter() - t0,
            "configs": results,
        }
        (root / f"{name}.json").write_text(
            json.dumps(doc, indent=1, default=repr), encoding="utf-8"
        )

    summary = report.write(root, names)
    print(f"study done in {time.perf_counter() - t_study:.1f}s -> {summary}", flush=True)


if __name__ == "__main__":
    main()
