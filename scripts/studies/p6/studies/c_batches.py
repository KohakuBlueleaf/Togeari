"""Study C: device batch sizes in ``examples/mixed`` against admission and the batch wait.

A larger source (the scale's ``batch_stations``), ``Fixed(n)`` for n in 32 / 64 / 128 / 256
crossed with ``SizeOrWait(64, wait_ms)`` for wait_ms in 2 / 5 / 10 / 20; no run report and no
recording, so the engines and the dispatcher alone set the timing. One configuration at a time
on ``cuda:0`` under the example's 2 GB cap.
"""

from pathlib import Path

from p6.common import measure
from p6.common.examples import example
from p6.common.report import slug, write_summary
from p6.scales import Scale
from togeari.policies import Fixed

TITLE = "Device batches: admission and batch wait against batch size"
ADMIT = (32, 64, 128, 256)
WAITS = (2, 5, 10, 20)


def plan(scale: Scale) -> list[dict]:
    return [
        {"name": f"Fixed({n}) wait {w} ms", "round": r, "admit": n, "wait_ms": w}
        for r in range(scale.gpu_rounds)
        for n in ADMIT
        for w in WAITS
    ]


def run(cfg: dict, scale: Scale, root: Path) -> dict:
    mixed = example("mixed")
    mixed.nodes.LOG["path"] = root / "logs" / f"{slug(cfg['name'])}.log"
    mixed.nodes.LOG["path"].parent.mkdir(parents=True, exist_ok=True)
    machine, source, sink = mixed.build(
        stations=scale.batch_stations,
        wait_ms=cfg["wait_ms"],
        report=None,
        admission=Fixed(cfg["admit"]),
        record=None,
    )
    res = measure.run(machine, source, sink)
    sizes = res["lanes"]["score"]["batch_sizes"]
    res["score_batch_mean"] = measure.batch_mean(sizes)
    res["score_batches"] = sum(sizes.values())
    return res


def scorer_busy(res: dict) -> float | None:
    return next((v for k, v in res["unit_busy"].items() if "scorer" in k), None)


def line(cfg: dict, res: dict) -> str:
    return (
        f"{res['seconds']:6.2f}s  {res['jobs_per_s']:6.1f} jobs/s  batch mean"
        f" {res['score_batch_mean'] or 0:5.1f}  scorer busy {scorer_busy(res) or 0:.2f}"
    )


def summarize(results: list[dict], root: Path) -> Path:
    rows = []
    inline: dict[str, list[float]] = {}
    for r in results:
        if "error" in r:
            rows.append([r["config"]["name"], "failed", None, None, None, None, None])
            continue
        x = r["result"]
        sizes = x["lanes"]["score"]["batch_sizes"]
        top = sorted(sizes.items(), key=lambda kv: -kv[1])[:3]
        rows.append(
            [
                r["config"]["name"],
                x["seconds"],
                x["jobs_per_s"],
                x["score_batch_mean"],
                ", ".join(f"{k}x{v}" for k, v in top),
                scorer_busy(x),
                x["peak_jobs"],
            ]
        )
        for node, p in x["inline"].items():
            inline.setdefault(node, []).append(p.get("p50_us", 0.0))

    header = [
        "config",
        "seconds",
        "jobs/s",
        "batch mean",
        "commonest sizes",
        "scorer busy",
        "peak jobs",
    ]
    times = "; ".join(f"`{n}` p50 {min(v):.1f}-{max(v):.1f} us" for n, v in sorted(inline.items()))
    notes = f"Batch sizes are the `score` lane's. Inline node run times (per-run p50): {times}."
    return write_summary(root, TITLE, header, rows, notes)
