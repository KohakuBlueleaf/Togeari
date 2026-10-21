"""Study P8, stage S8: every spec/11 §11.6 measurement for every variant, in one run.

One process at a time: the trace parity tests (a failure stops the run); the dispatcher
ceiling with the variants interleaved inside each round; scaling and P6 studies A (with
``examples/mixed`` on ``cuda:0``) and B, one round per child, every variant in each round with the
order rotating per round; the GIL study (rounds outermost, rotating too); then ``summary.md`` and
``s8.json``: the decision rule and its verdicts, every failure, every table, the environment,
every child's command, log, exit code and seconds.

Every child has a timeout; a child that times out or crashes is a failure of its variant, and the
run goes on to write its report. An earlier run's output folder is moved aside first (to
``<scale>-prev-<time>``, never deleted), so nothing of it can be read as this run's.

Run from the repository root: ``.venv/Scripts/python.exe scripts/studies/p8/s8.py --scale
full``. Output under ``runs/p8/s8/<scale>/``.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

STUDIES = Path(__file__).resolve().parents[1]
REPO = STUDIES.parents[1]
sys.path.insert(0, str(STUDIES))

from p6.scales import SCALES as P6_SCALES  # noqa: E402

from p8 import baseline, gil  # noqa: E402
from p8.scales import SCALES as GIL_SCALES  # noqa: E402
from p8.summary import collect, decide, env, expected, render  # noqa: E402
from p8.variants import ALL, pick, rotated  # noqa: E402
from togeari.core.construct.core import native, rust_available  # noqa: E402

# Per scale: ceiling rounds, the P6 studies' scale, the GIL study's scale, child timeouts (s)
SCALES = {
    "small": {
        "rounds": 1,
        "p6": "small",
        "gil": "small",
        "timeouts": {"parity": 1800, "ceiling": 1800, "scaling": 600, "p6-A": 900, "p6-B": 900},
    },
    "full": {
        "rounds": 5,
        "p6": "full",
        "gil": "full",
        "timeouts": {"parity": 1800, "ceiling": 3600, "scaling": 900, "p6-A": 1800, "p6-B": 1800},
    },
}

# Scaling runs per child: discarded warm-up runs, then measured ones
SCALING_ARGS = ["--warmup", "1", "--repeats", "2"]


def fresh(root: Path) -> Path | None:
    """Move an earlier run's folder aside (never deleted); where it went, or None."""
    if not root.exists():
        return None
    aside = root.with_name(f"{root.name}-prev-{time.strftime('%Y%m%d-%H%M%S')}")
    root.rename(aside)
    return aside


def ceiling(variants: dict, rounds: int, root: Path, timeout: float) -> dict:
    """Every variant inside each round of one ceiling process."""
    out = root / "ceiling.json"
    out.unlink(missing_ok=True)
    cmd = [
        str(REPO / "scripts" / "benches" / "dispatcher_ceiling.py"),
        str(out),
        str(rounds),
        "--variants",
        json.dumps(variants),
    ]
    child_env = {k: v for k, v in os.environ.items() if k != "TOGEARI_CORE"}
    child_env |= {**baseline.GPU_ENV, "PYTHONUNBUFFERED": "1"}
    log = root / "logs" / "ceiling.log"
    return {
        "name": "ceiling (all variants)",
        "command": cmd,
        **baseline.run_child(cmd, child_env, log, timeout),
        "log": str(log.relative_to(REPO)),
    }


def parity(root: Path, timeout: float) -> dict:
    """The trace parity tests (both cores, decision for decision), before anything is measured."""
    cmd = ["-m", "pytest", "tests/testing/trace", "-q", "-p", "no:cacheprovider"]
    log = root / "logs" / "parity.log"
    child_env = {**os.environ, **baseline.GPU_ENV, "PYTHONUNBUFFERED": "1"}
    child = baseline.run_child(cmd, child_env, log, timeout)
    lines = [x for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {
        "name": "parity (trace tests)",
        "command": " ".join(cmd),
        "passed": child["exit_code"] == 0,
        "tail": lines[-1] if lines else "",
        **child,
        "log": str(log.relative_to(REPO)),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="P8 S8: every core measurement, one run")
    p.add_argument("--scale", choices=sorted(SCALES), default="small")
    p.add_argument("--variants", default=ALL)
    p.add_argument("--out", default=str(REPO / "runs" / "p8" / "s8"))
    args = p.parse_args()

    variants = pick(args.variants)
    if any(v["core"] == "rust" for v in variants.values()):
        if not rust_available():
            raise SystemExit("the Rust core is not built: maturin develop --release first")
        if native().core_info()["debug"]:
            raise SystemExit("togeari._rs is a debug build: maturin develop --release first")
    scale = SCALES[args.scale]
    root = Path(args.out) / args.scale
    aside = fresh(root)
    if aside is not None:
        print(f"earlier output moved to {aside}", flush=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    t_run = time.perf_counter()
    steps = []
    info = env.collect(REPO)
    timeouts = scale["timeouts"]

    # 0. Parity first: a core that decides differently is not measured
    checked = parity(root, timeouts["parity"])
    steps.append(checked)
    print(f"parity {checked['tail']}", flush=True)
    if not checked["passed"]:
        raise SystemExit(f"parity failed, nothing measured; see {checked['log']}")

    # 1. The ceiling, interleaved
    steps.append(ceiling(variants, scale["rounds"], root, timeouts["ceiling"]))
    print(f"ceiling exit {steps[-1]['exit_code']}  {steps[-1]['seconds']:.1f}s", flush=True)

    # 2. Scaling and P6 A and B: one round per child, every variant per round, rotating order
    names = list(variants)
    p6_rounds = P6_SCALES[scale["p6"]].rounds
    for rnd in range(p6_rounds):
        for name in rotated(names, rnd):
            manifest = baseline.run_variant(
                name,
                variants[name],
                root / "baseline",
                ["scaling", "p6-A", "p6-B"],
                scale["p6"],
                scale["rounds"],
                rnd=rnd,
                timeouts=timeouts,
                scaling_args=SCALING_ARGS,
            )
            tags = (f"scaling-r{rnd}", f"p6-A-r{rnd}", f"p6-B-r{rnd}")
            steps += [
                {"name": f"{name} {tag}", "variant": name, **manifest["benches"][tag]}
                for tag in tags
            ]

    # 3. The GIL study
    results, _ = gil.run_study(variants, scale["gil"], root / "gil")
    for r in results:
        cfg = r["config"]
        child = r.get("child") or {}
        steps.append(
            {
                "name": f"gil r{cfg.get('round')} {cfg.get('variant')} {cfg['work']}"
                f" {cfg['isolation']}",
                "variant": cfg.get("variant"),
                "exit_code": child.get("exit_code", r.get("skipped") or r.get("error")),
                "seconds": child.get("seconds", 0.0),
                "log": str(Path(child["log"]).relative_to(REPO)) if child.get("log") else "",
            }
        )

    # 4. Side by side, against the shapes the plan expects
    gil_loads = [n for n, _ in GIL_SCALES[scale["gil"]].loads]
    scaling, scaling_native = collect.scaling(root / "baseline", names)
    data = {
        "ceiling": collect.ceiling(root / "ceiling.json"),
        "scaling": scaling,
        "scaling_native": scaling_native,
        "p6": {s: collect.p6(root / "baseline", names, scale["p6"], s) for s in ("A", "B")},
        "gil": collect.gil(results, gil_loads),
    }
    shapes = expected.shapes(scale["p6"], scale["gil"])
    failed = collect.failures(data, steps, names)
    compared = decide.compare(data, shapes, decide.pairs_of(names))
    doc = {
        "scale": args.scale,
        "rounds": scale["rounds"],
        "p6_rounds": p6_rounds,
        "variants": variants,
        "env": info,
        "moved_aside": str(aside) if aside else None,
        "parity": checked,
        "gil_loads": gil_loads,
        "steps": steps,
        "seconds": time.perf_counter() - t_run,
        "rule": decide.RULE,
        "failures": failed,
        "verdicts": decide.verdicts(compared, checked, data, failed),
        "expected": shapes,
        "compare": compared,
        "data": data,
    }
    (root / "s8.json").write_text(json.dumps(doc, indent=1, default=repr), encoding="utf-8")
    summary = root / "summary.md"
    summary.write_text(render.markdown(doc), encoding="utf-8")
    for name, v in doc["verdicts"].items():
        print(f"{name} = {v['value']}: {v['reason']}", flush=True)
    print(f"S8 done in {doc['seconds']:.1f}s -> {summary}", flush=True)


if __name__ == "__main__":
    main()
