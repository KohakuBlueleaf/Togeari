"""Dispatcher ceiling on CPU: statements per second with engines that cost nothing.

Three shapes, each timed with every dispatcher phase: inline-only chains, the
same chain on a zero-latency call lane, and a wide `each` on a zero-latency
batch lane. Each round runs every shape under both collector settings (the
``gc_threshold`` default and the process's own threshold), one after the other,
for every variant. Writes one JSON report with every run and, per variant, shape
and setting, the median, best and worst calls/s.

Usage: ``python dispatcher_ceiling.py [out.json] [rounds=5] [--knobs JSON]
[--variants JSON]``. ``--knobs`` are applied to every machine (under the collector
setting); ``--variants`` is ``{name: {"core": ..., "knobs": {...}}}``, interleaved
inside each round so drift reaches every variant alike (default: one variant,
``default``, on the default core).
"""

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.io import Iterable, Memory
from togeari.policies import Fixed, SizeOrWait
from togeari.testing import SimBatch, SimCall

lib = NodeLibrary("bench")


@lib.node()
def inc(x: int) -> int:
    return x + 1


CHAIN = (
    "@def (x: int)main(y):\n"
    + "".join(f"    ({'x' if i == 0 else f'v{i - 1}'})inc(v{i})\n" for i in range(20))
    + "    y = v19\n"
)

WIDE = """
@def (n: int)main(total):
    (n)range(items)
    total = 0
    (items)each(i, `one`)
    one:
        (i)inc(j)
        (total, j)add(total)
"""


def bench(name: str, program: str, bind: dict, jobs: list, **knobs) -> dict:
    m = Machine(
        program=program,
        entry="main",
        libraries=[lib],
        bind=bind,
        admission=Fixed(256),
        **knobs,
    )

    # One run, timed; the dispatcher's counters read before stop
    try:
        sink = Memory()
        t0 = time.perf_counter()
        summary = m.run(Iterable(jobs), sink)
        dt = time.perf_counter() - t0
        stats = m.inspect("stats").result()
        native = m.dispatcher.native_stats() if m.core == "rust" else None
    finally:
        m.stop()

    calls = stats["calls"]
    return {
        "shape": name,
        "core": m.core,
        "knobs": {k: v for k, v in knobs.items() if k != "core"},
        "gc_threshold": m.knobs["gc_threshold"],
        "jobs": len(jobs),
        "calls": calls,
        "seconds": dt,
        "calls_per_s": calls / dt,
        "jobs_per_s": len(jobs) / dt,
        "dispatcher_busy": summary["dispatcher_busy"],
        "phase_s": stats["phase_s"],
        "cycles": stats["cycles"],
        "native_stats": native,
        "ok": sum(r.ok for r in sink.records),
    }


def _inline() -> dict:
    return {"inc": Inline()}


def _call() -> dict:
    return {"inc": Use(Lane([SimCall(name="c", latency_ms=0.0, capacity=64)]))}


def _batch() -> dict:
    lane = Lane([SimBatch(name="b", latency_ms=0.0, max_batch=64)], former=SizeOrWait(64, 1))
    return {"inc": Use(lane), "kir.std.*": Inline()}


# Each shape: (program, fresh bindings, jobs)
SHAPES = {
    # Inline only: the front end, issue and retire alone
    "chain-inline": (CHAIN, _inline, [{"x": i} for i in range(2000)]),
    # A zero-latency call lane: lanes and the inbox round trip
    "chain-call": (CHAIN, _call, [{"x": i} for i in range(1000)]),
    # A wide `each` on a zero-latency batch lane: strands, phis, batching
    "each-batch": (WIDE, _batch, [{"n": 50} for _ in range(200)]),
}

# Collector settings, each run once per shape per round: the knob default, the process's own
SETTINGS = {"default": {}, "process": {"gc_threshold": None}}


def run_shape(name: str, **knobs) -> dict:
    program, bind, jobs = SHAPES[name]
    return bench(name, program, bind(), jobs, **knobs)


def summarize(rows: list[dict]) -> dict:
    """Median, best and worst calls/s of one shape and setting, with the median row's figures."""
    rates = sorted(r["calls_per_s"] for r in rows)
    mid = sorted(rows, key=lambda r: r["calls_per_s"])[len(rows) // 2]
    return {
        "median": statistics.median(rates),
        "best": rates[-1],
        "worst": rates[0],
        "jobs_per_s": mid["jobs_per_s"],
        "dispatcher_busy": mid["dispatcher_busy"],
        "ok": all(r["ok"] == r["jobs"] for r in rows),
    }


def summary_of(rows: list[dict]) -> dict:
    """Per shape and setting, the summary of ``rows`` (one variant's)."""
    return {
        name: {
            setting: summarize([r for r in rows if r["shape"] == name and r["setting"] == setting])
            for setting in SETTINGS
        }
        for name in SHAPES
    }


def main(out: str, rounds: int, knobs: dict, variants: dict) -> None:
    # Rounds interleave every variant and setting per shape, so none runs on a fresher process;
    # the variant order rotates by one each round, so none always runs first
    rows = []
    names = list(variants)
    for n in range(rounds):
        order = names[n % len(names) :] + names[: n % len(names)]
        for name in SHAPES:
            for variant in order:
                v = variants[variant]
                for setting, own in SETTINGS.items():
                    run = run_shape(name, core=v["core"], **{**knobs, **v["knobs"], **own})
                    rows.append({"round": n, "variant": variant, "setting": setting, **run})

    by_variant = {v: summary_of([r for r in rows if r["variant"] == v]) for v in variants}
    report = {
        "python": sys.version,
        "machine": platform.platform(),
        "rounds": rounds,
        "knobs": knobs,
        "variants": variants,
        "by_variant": by_variant,
        "rows": rows,
    }
    if len(variants) == 1:
        report["summary"] = next(iter(by_variant.values()))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, indent=2))

    # One line per variant, shape and setting: calls/s over the rounds, the gain of the default
    for variant, summary in by_variant.items():
        for name, by_setting in summary.items():
            for setting, s in by_setting.items():
                print(
                    f"{variant:<10} {name:<14} {setting:<8} median {s['median']:>8.0f}"
                    f"  best {s['best']:>8.0f}  worst {s['worst']:>8.0f} calls/s"
                    f"  {s['jobs_per_s']:>7.1f} jobs/s  busy {s['dispatcher_busy']:.2f}"
                    f"  all ok {s['ok']}"
                )
            gain = by_setting["default"]["median"] / by_setting["process"]["median"] - 1
            print(f"{variant:<10} {name:<14} default over process: {gain:+.0%} (median)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Dispatcher ceiling on CPU")
    p.add_argument("out", nargs="?", default="runs/bench/dispatcher_ceiling.json")
    p.add_argument("rounds", nargs="?", type=int, default=5)
    p.add_argument("--knobs", default="{}", help="JSON knobs for every machine")
    p.add_argument("--variants", default=None, help='JSON {name: {"core": ..., "knobs": {...}}}')
    args = p.parse_args()
    main(
        args.out,
        args.rounds,
        json.loads(args.knobs),
        (json.loads(args.variants) if args.variants else {"default": {"core": None, "knobs": {}}}),
    )
