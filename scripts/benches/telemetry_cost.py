"""What telemetry costs the dispatcher: the same machines with and without it, in one report.

Variants (each run in a fresh process; ``--old-python`` is an interpreter with another build of
Togeari installed, the baseline):

- ``old``: the baseline build, no plugin; ``off``: this build, no plugin;
- ``ring`` / ``ring-process``: the ring attached, drained and discarded
  (``Telemetry(store=False)``) by a thread / by the writer's process;
- ``thread`` / ``process``: the ring and the writer on a thread of the machine's process / in
  a process of its own (``Telemetry(writer=...)``);
- ``process+query``: the process writer while a reader queries the directory every second, as
  the dashboard does (series, a ranking, a histogram, the traces);
- ``old+inspector`` / ``inspector``: the inspector loaded with no stream open.

Shapes: the three of ``dispatcher_ceiling.py`` (the dispatcher saturated: cost shows as calls
per second) and ``pipeline`` (three lanes with latency, the dispatcher not saturated: cost shows
as its busy fraction). Each round runs every shape, core and variant once, the variant order
rotated per round. The report holds every run (rates, busy fraction, phase times, the ring's
counts, every writer tick time) and per (shape, core, variant) the median, best and worst.

Each run also reports its stop (the writer's last tick and file closes) and the rate with it,
the GIL probe's overshoot (``--probe-ms``; 0 runs without the probe), and the ring's drops (a
dropped record is never written, so a run with drops is cheaper than one without).

Usage: ``python telemetry_cost.py out.json [rounds] [--scale N] [--old-python PATH]
[--variants a,b] [--cores python,rust] [--shapes a,b] [--probe-ms 0.5]``.
"""

import argparse
import json
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.io import Iterable, Memory
from togeari.plugins import Inspector
from togeari.policies import Fixed, SizeOrWait
from togeari.testing import SimBatch, SimCall

sys.path.insert(0, str(Path(__file__).parent))
import dispatcher_ceiling as ceiling

try:
    from togeari.plugins.telemetry import Telemetry
    from togeari.plugins.telemetry.query import Run
except ImportError:
    Telemetry = Run = None

VARIANTS = (
    "old",
    "off",
    "ring",
    "ring-process",
    "thread",
    "process",
    "process+query",
    "old+inspector",
    "inspector",
)
OLD = ("old", "old+inspector")

# Variant -> the telemetry plugin's knobs (no entry: no plugin)
TELEMETRY = {
    "ring": {"writer": "thread", "store": False},
    "ring-process": {"writer": "process", "store": False},
    "thread": {"writer": "thread"},
    "process": {"writer": "process"},
    "process+query": {"writer": "process"},
}

lib = NodeLibrary("pipe")


@lib.node()
def fetch(x: int) -> int:
    return x + 1


@lib.node()
def embed(x: int) -> int:
    return x * 2


@lib.node()
def caption(x: int) -> int:
    return x - 1


PIPE = """
@def (x: int)main(y):
    (x)fetch(a)
    (a)embed(b)
    (b)caption(c)
    (c, a)add(d)
    (d)embed(e)
    (e, b)add(y)
"""


def _pipe() -> dict:
    fast = Lane(
        [SimCall(name=f"f{i}", latency_ms=2.0, capacity=32) for i in range(2)], name="fetch"
    )
    gpu = Lane(
        [SimBatch(name=f"g{i}", latency_ms=12.0, max_batch=32) for i in range(2)],
        former=SizeOrWait(32, 5),
        name="embed",
    )
    llm = Lane([SimCall(name="l0", latency_ms=8.0, capacity=64)], name="caption")
    return {
        "fetch": Use(fast),
        "embed": Use(gpu),
        "caption": Use(llm),
        "kir.std.*": Inline(),
    }


SHAPES = {
    **{k: (p, b, j, ceiling.lib) for k, (p, b, j) in ceiling.SHAPES.items()},
    "pipeline": (PIPE, _pipe, [{"x": i} for i in range(3000)], lib),
}


# ------------------------------------------------------------ one run (child process)
def querier(path: Path, stop: threading.Event, times: list) -> None:
    """The dashboard's reads, once a second, each call timed."""
    run = Run(path)
    while not stop.wait(1.0):
        t0 = time.perf_counter()
        try:
            span = run.span()
            if span is not None:
                lo, hi = span
                top = run.top("node", "calls", lo, hi, "count", 5)
                for r in top[:2]:
                    run.series(r["entity"], "calls", lo, hi, 600, "rate")
                    run.hist(r["entity"], "calls", lo, hi)
                run.traces(limit=50)
        except Exception as exc:  # noqa: BLE001
            times.append({"error": repr(exc)})
            continue
        times.append((time.perf_counter() - t0) * 1e3)


def probe(stop: threading.Event, waits: list, sleep_ms: float) -> None:
    """A thread asking for a ``sleep_ms`` sleep over and over: its overshoot is how long it
    waited for the GIL (and the scheduler), the longest hold of any other thread. Each wake
    takes the GIL, so the probe itself costs the dispatcher a little (``--probe-ms 0``: none)."""
    while not stop.is_set():
        t = time.perf_counter()
        time.sleep(sleep_ms / 1e3)
        waits.append((time.perf_counter() - t - sleep_ms / 1e3) * 1e3)


def child(spec: dict) -> dict:
    program, bind, jobs, library = SHAPES[spec["shape"]]
    jobs = jobs * spec["scale"]
    variant = spec["variant"]
    tmp = Path(tempfile.mkdtemp(prefix="tel-cost-"))
    plugins = []
    tel = None
    if variant in TELEMETRY:
        tel = Telemetry(tmp / "tel", **TELEMETRY[variant])
        plugins.append(tel)
    if variant.endswith("inspector"):
        plugins.append(Inspector(token=None))
    m = Machine(
        program=program,
        entry="main",
        libraries=[library],
        bind=bind(),
        admission=Fixed(256),
        plugins=plugins,
        core=spec["core"],
    )

    # The reader and the GIL probe start with the run; the run is timed; counters are read
    # before stop
    stop = threading.Event()
    qtimes: list = []
    waits: list = []
    threads = []
    if spec["probe_ms"] > 0:
        threads.append(
            threading.Thread(target=probe, args=(stop, waits, spec["probe_ms"]), daemon=True)
        )
    if variant.endswith("+query"):
        threads.append(
            threading.Thread(target=querier, args=(tmp / "tel", stop, qtimes), daemon=True)
        )
    for t in threads:
        t.start()
    try:
        sink = Memory()
        t0 = time.perf_counter()
        summary = m.run(Iterable(jobs), sink)
        dt = time.perf_counter() - t0
        stop.set()
        stats = m.inspect("stats").result()
        status = m.inspect("telemetry").result() if tel is not None else None
    finally:
        stop.set()

        # Stop is timed too: the writer's last tick and file closes run here
        t1 = time.perf_counter()
        m.stop()
        stop_s = time.perf_counter() - t1
    if tel is not None:
        status = tel.status()
        if tel.store:
            status["sample_us"] = Run(tmp / "tel").samples(["telemetry/sample_us"], 0, 1e12)
            status["sample_us"] = status["sample_us"].get("telemetry/sample_us", {}).get("v", [])
    for t in threads:
        t.join(timeout=10)
    waits.sort()
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        **spec,
        "jobs": len(jobs),
        "calls": stats["calls"],
        "seconds": dt,
        "stop_s": stop_s,
        "calls_per_s": stats["calls"] / dt,
        "calls_per_s_with_stop": stats["calls"] / (dt + stop_s),
        "jobs_per_s": len(jobs) / dt,
        "dispatcher_busy": summary["dispatcher_busy"],
        "phase_s": stats["phase_s"],
        "cycles": stats["cycles"],
        "cycle_hist": stats.get("cycle_hist", {}),
        "cycle_max_us": stats.get("cycle_max_s", 0.0) * 1e6,
        "gil_wait_ms": {
            "n": len(waits),
            **{f"p{int(p * 1000) / 10:g}": pct(waits, p) for p in (0.5, 0.9, 0.99, 0.999)},
            "max": waits[-1] if waits else None,
        },
        "ok": sum(r.ok for r in sink.records),
        "telemetry": status,
        "query_ms": qtimes,
    }


# ------------------------------------------------------------ the report (parent process)
def pct(xs: list, p: float):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def summarize(rows: list[dict]) -> dict:
    rates = sorted(r["calls_per_s"] for r in rows)
    with_stop = sorted(r["calls_per_s_with_stop"] for r in rows)
    stops = sorted(r["stop_s"] * 1e3 for r in rows)
    busy = sorted(r["dispatcher_busy"] for r in rows)
    ticks = [t for r in rows if r["telemetry"] for t in r["telemetry"]["writer"]["ticks_ms"]]
    samples = sorted(u for r in rows if r["telemetry"] for u in r["telemetry"].get("sample_us", []))
    queries = [q for r in rows for q in r["query_ms"] if isinstance(q, float)]
    ring = [r["telemetry"]["ring"] for r in rows if r["telemetry"]]
    gil99 = sorted(r["gil_wait_ms"]["p99"] for r in rows if r["gil_wait_ms"]["p99"] is not None)
    gilmax = sorted(r["gil_wait_ms"]["max"] for r in rows if r["gil_wait_ms"]["max"] is not None)
    cycle = sorted(r["cycle_max_us"] for r in rows)
    return {
        "runs": len(rows),
        "calls_per_s": {"median": statistics.median(rates), "best": rates[-1], "worst": rates[0]},
        "calls_per_s_with_stop": {
            "median": statistics.median(with_stop),
            "best": with_stop[-1],
            "worst": with_stop[0],
        },
        "stop_ms": {"median": statistics.median(stops), "max": stops[-1]},
        "busy": {"median": statistics.median(busy), "min": busy[0], "max": busy[-1]},
        "gil_wait_p99_ms": {"median": pct(gil99, 0.5), "max": gil99[-1] if gil99 else None},
        "gil_wait_max_ms": {"median": pct(gilmax, 0.5), "max": gilmax[-1] if gilmax else None},
        "cycle_max_us": {"median": pct(cycle, 0.5), "max": cycle[-1]},
        "all_ok": all(r["ok"] == r["jobs"] for r in rows),
        "writer_tick_ms": {
            "n": len(ticks),
            **{f"p{int(p * 100)}": pct(ticks, p) for p in (0.5, 0.9, 0.99)},
            "max": max(ticks) if ticks else None,
        },
        "dispatcher_sample_us": {
            "n": len(samples),
            **{f"p{int(p * 100)}": pct(samples, p) for p in (0.5, 0.9, 0.99)},
            "max": samples[-1] if samples else None,
        },
        "query_ms": {
            "n": len(queries),
            "p50": pct(queries, 0.5),
            "max": max(queries, default=None),
        },
        "ring_dropped": sum(c["dropped"] for c in ring),
        "ring_written": sum(c["written"] for c in ring),
    }


def main(args: argparse.Namespace) -> None:
    variants = args.variants.split(",")
    if args.old_python is None and any(v in OLD for v in variants):
        variants = [v for v in variants if v not in OLD]
        print(f"no --old-python: the baseline variants {OLD} are left out", file=sys.stderr)
    cores = args.cores.split(",")
    shapes = args.shapes.split(",")
    rows = []
    for n in range(args.rounds):
        order = variants[n % len(variants) :] + variants[: n % len(variants)]
        for shape in shapes:
            for core in cores:
                for variant in order:
                    py = args.old_python if variant in OLD else sys.executable
                    spec = {
                        "shape": shape,
                        "core": core,
                        "variant": variant,
                        "scale": args.scale,
                        "probe_ms": args.probe_ms,
                    }
                    out = subprocess.run(
                        [py, __file__, "--child", json.dumps(spec)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if out.returncode != 0:
                        print(out.stderr[-2000:], file=sys.stderr)
                        raise SystemExit(f"run {spec} failed")
                    row = json.loads(out.stdout.strip().splitlines()[-1])
                    rows.append({"round": n, **row})
                    print(
                        f"r{n} {shape:<13} {core:<6} {variant:<14}"
                        f" {row['calls_per_s']:>9.0f} calls/s  busy {row['dispatcher_busy']:.3f}",
                        flush=True,
                    )

    summary = {
        shape: {
            core: {
                v: summarize(
                    [r for r in rows if (r["shape"], r["core"], r["variant"]) == (shape, core, v)]
                )
                for v in variants
            }
            for core in cores
        }
        for shape in shapes
    }
    report = {
        "python": sys.version,
        "machine": platform.platform(),
        "rounds": args.rounds,
        "scale": args.scale,
        "variants": variants,
        "summary": summary,
        "rows": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=1))
    for shape, by_core in summary.items():
        for core, by_v in by_core.items():
            base = by_v.get("off", {}).get("calls_per_s", {}).get("median")
            for v, s in by_v.items():
                rel = f"{s['calls_per_s']['median'] / base - 1:+.1%}" if base else ""
                tick = s["writer_tick_ms"]["p99"]
                print(
                    f"{shape:<13} {core:<6} {v:<14} median {s['calls_per_s']['median']:>9.0f}"
                    f" [{s['calls_per_s']['worst']:.0f}, {s['calls_per_s']['best']:.0f}] {rel:>7}"
                    f"  busy {s['busy']['median']:.3f}"
                    f"  gil p99 {s['gil_wait_p99_ms']['median']:.2f} ms"
                    f"  cycle max {s['cycle_max_us']['median']:.0f} us"
                    f"  tick p99 {'-' if tick is None else f'{tick:.1f}'} ms"
                    f"  sample p99 {s['dispatcher_sample_us']['p99'] or 0:.0f} us"
                    f"  dropped {s['ring_dropped']}"
                )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="What telemetry costs the dispatcher")
    p.add_argument("out", nargs="?", default="runs/bench/telemetry_cost.json")
    p.add_argument("rounds", nargs="?", type=int, default=5)
    p.add_argument("--scale", type=int, default=10)
    p.add_argument("--old-python", default=None)
    p.add_argument("--variants", default=",".join(VARIANTS))
    p.add_argument("--cores", default="python,rust")
    p.add_argument("--shapes", default=",".join(SHAPES))
    p.add_argument("--probe-ms", type=float, default=0.5)
    p.add_argument("--child", default=None)
    a = p.parse_args()
    if a.child is not None:
        print(json.dumps(child(json.loads(a.child))))
    else:
        main(a)
