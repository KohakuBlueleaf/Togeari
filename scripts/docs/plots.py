"""The README's plots, as SVG files with no plotting dependency.

- ``parallel-timeline.svg``: one job of ``examples/parallel`` (each call where it ran) above
  the same calls laid one after another;
- ``parallel-speedup.svg``: the machine's time against the summed call time, one job and
  four jobs;
- ``dispatcher-ceiling.svg``: node calls per second of the ceiling shapes, from a
  ``dispatcher_ceiling.py`` report;
- with ``--s8 DIR`` (a ``scripts/studies/p8/s8.py`` output, e.g. ``runs/p8/s8/full``), only
  the cores compared: ``cores-ceiling.svg``, ``cores-scaling.svg``, ``cores-gil.svg``.

Usage: ``python scripts/docs/plots.py [ceiling.json] [out_dir] [--s8 DIR]``.
"""

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PARALLEL = ROOT / "examples" / "parallel"

# Colours readable on light and dark pages
INK = "#8b949e"
GRID = "#8b949e33"
MACHINE = "#2f81f7"
SEQUENTIAL = "#d29922"
BRANCH = "#a371f7"
# One colour per core variant of the S8 figures
VARIANT = {
    "python": SEQUENTIAL,
    "rust-o0": MACHINE,
    "rust-o20": "#3fb950",
    "rust-o100": "#1f9e89",
    "rust-o300": "#db61a2",
    "rust-o1000": "#f0883e",
    "rust-o5000": "#e5534b",
    "rust-oinf": "#6e7681",
    "rust-onone": BRANCH,
}
# The variant the defaults run (core "auto", gil_offer_us None), drawn heavier
DEFAULT_VARIANT = "rust-onone"


def variant_label(name: str) -> str:
    """``rust-o300`` as ``rust 300 us``, the default variant marked."""
    if not name.startswith("rust-o"):
        return name
    value = name.removeprefix("rust-o")
    value = {"none": "None", "inf": "inf"}.get(value, f"{value} µs")
    mark = " (default)" if name == DEFAULT_VARIANT else ""
    return f"rust {value}{mark}"
SHAPE_NAMES = {
    "chain-inline": "chain of inline nodes",
    "chain-call": "chain of lane calls",
    "each-batch": "each with batched calls",
}
FONT = "font-family='ui-sans-serif, -apple-system, Segoe UI, Helvetica, Arial' font-size='13'"


def load_parallel():
    """The parallel example's module and its node library (TIMELINE)."""
    sys.path.insert(0, str(PARALLEL))
    spec = importlib.util.spec_from_file_location("parallel_machine", PARALLEL / "machine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, sys.modules["nodes"]


def svg(width: int, height: int, body: list[str]) -> str:
    head = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}' {FONT}>"
    )
    return "\n".join([head, *body, "</svg>\n"])


def text(x: float, y: float, s: str, anchor: str = "start", color: str = INK) -> str:
    return f"<text x='{x:.1f}' y='{y:.1f}' fill='{color}' text-anchor='{anchor}'>{s}</text>"


def rect(x: float, y: float, w: float, h: float, color: str) -> str:
    size = f"width='{max(w, 1):.1f}' height='{h:.1f}'"
    return f"<rect x='{x:.1f}' y='{y:.1f}' {size} rx='3' fill='{color}'/>"


def axis(x0: float, x1: float, y: float, top: float, end_s: float, step_s: float) -> list[str]:
    """Second ticks along ``y`` with grid lines up to ``top``."""
    out = [f"<line x1='{x0}' y1='{y}' x2='{x1}' y2='{y}' stroke='{INK}'/>"]
    n = int(end_s / step_s) + 1
    for i in range(n):
        t = i * step_s
        x = x0 + (x1 - x0) * t / end_s
        out.append(f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{y}' stroke='{GRID}'/>")
        out.append(text(x, y + 18, f"{t:.1f} s", "middle"))
    return out


def timeline(calls: list[tuple[str, float, float]], out: Path) -> None:
    """Rows of calls as they ran, then the same calls one after another."""
    row, left, right = 22, 150, 880
    rows = len(calls)
    seq_total = sum(stop - start for _, start, stop in calls)
    end_s = seq_total
    top = 40
    height = top + row * (rows + 2) + 70

    def x(t: float) -> float:
        return left + (right - left) * t / end_s

    body = [text(10, 22, "one job of examples/parallel: when each call ran", color=INK)]

    # The machine: each call at its real time
    for i, (node, start, stop) in enumerate(calls):
        y = top + i * row
        color = BRANCH if node == "alert" else MACHINE
        body.append(text(left - 8, y + 14, node, "end"))
        body.append(rect(x(start), y + 3, x(stop) - x(start), row - 6, color))

    # The same calls one after another
    y = top + rows * row + 8
    body.append(text(left - 8, y + 14, "one after another", "end"))
    t = 0.0
    for _, start, stop in calls:
        body.append(rect(x(t), y + 3, x(t + stop - start) - x(t) - 1, row - 6, SEQUENTIAL))
        t += stop - start
    machine_end = max(stop for _, _, stop in calls)
    body.append(text(x(machine_end) + 6, top + 14, f"machine: {machine_end:.2f} s"))
    body.append(text(x(seq_total) - 4, y - 4, f"{seq_total:.2f} s", "end"))
    body += axis(left, right, y + row + 6, top, end_s, 0.2)
    out.write_text(svg(900, height, body), encoding="utf-8")


def bars(title: str, rows: list[tuple[str, list[tuple[float, str, str]]]], unit: str, out: Path):
    """Horizontal grouped bars: each row has (value, label, colour) bars."""
    row, left, right = 26, 230, 820
    top = 40
    n = sum(len(b) for _, b in rows)
    height = top + n * row + len(rows) * 10 + 20
    vmax = max(v for _, b in rows for v, _, _ in b)
    body = [text(10, 22, title)]
    y = top
    for label, group in rows:
        body.append(text(left - 10, y + 16 + (len(group) - 1) * row / 2, label, "end"))
        for value, name, color in group:
            w = (right - left) * value / vmax
            body.append(rect(left, y + 4, w, row - 8, color))
            body.append(text(left + w + 6, y + 18, f"{value:,.2f} {unit}  {name}".strip()))
            y += row
        y += 10
    out.write_text(svg(1000, height, body), encoding="utf-8")


def main(ceiling_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    module, nodes = load_parallel()
    problems: list[str] = []

    # One job: the timeline and its totals
    _, t0, one_elapsed, one_calls = module.run(module.CITIES[:1], problems)
    calls = sorted(
        ((node, start - t0, stop - t0) for _, node, start, stop in nodes.TIMELINE),
        key=lambda c: c[1],
    )
    timeline(calls, out_dir / "parallel-timeline.svg")

    # Four jobs
    _, _, four_elapsed, four_calls = module.run(module.CITIES, problems)
    if problems:
        raise SystemExit(f"the example failed: {problems}")
    bars(
        "examples/parallel: the machine against the same calls one after another",
        [
            ("one job", [(one_elapsed, "machine", MACHINE), (one_calls, "sequential", SEQUENTIAL)]),
            (
                "four jobs",
                [(four_elapsed, "machine", MACHINE), (four_calls, "sequential", SEQUENTIAL)],
            ),
        ],
        "s",
        out_dir / "parallel-speedup.svg",
    )

    # The dispatcher ceiling, default collector knob and the process's own
    report = json.loads(ceiling_path.read_text(encoding="utf-8"))
    rows = [
        (
            SHAPE_NAMES[shape],
            [
                (s["default"]["median"] / 1000, "default", MACHINE),
                (s["process"]["median"] / 1000, "gc_threshold=None", SEQUENTIAL),
            ],
        )
        for shape, s in report["summary"].items()
    ]
    bars(
        "dispatcher ceiling: thousand node calls per second",
        rows,
        "k/s",
        out_dir / "dispatcher-ceiling.svg",
    )
    print(f"one job {one_elapsed:.2f}s vs {one_calls:.2f}s")
    print(f"four jobs {four_elapsed:.2f}s vs {four_calls:.2f}s")
    print(f"plots in {out_dir}")


# ------------------------------------------------------------------ the cores compared (S8)
def legend(x: float, y: float, names: list[str], width: float) -> tuple[list[str], float]:
    """A swatch and label per variant, wrapped at ``width``; returns the parts and the y below."""
    out = []
    x0 = x
    for name in names:
        label = variant_label(name)
        step = 30 + 7.5 * len(label)
        if x + step > x0 + width:
            x, y = x0, y + 22
        out.append(rect(x, y - 10, 14, 10, VARIANT.get(name, INK)))
        out.append(text(x + 20, y, label))
        x += step
    return out, y + 22


def nice_top(v: float) -> float:
    """The smallest round top at or above ``v`` whose quarters are round too."""
    p = 10 ** math.floor(math.log10(v))
    return next(k * p for k in (1, 1.2, 1.6, 2, 4, 6, 8, 10) if k * p >= v)


def panel(
    x0: float,
    y0: float,
    w: float,
    h: float,
    title: str,
    xs: list[str],
    series: dict[str, list[float | None]],
    y_label: str,
    log_y: bool = False,
) -> list[str]:
    """One line chart: ``xs`` evenly spaced, one line per series (None leaves a gap)."""
    vals = [v for s in series.values() for v in s if v is not None and v > 0]
    body = [text(x0, y0 - 8, title)]
    if not vals:
        return body + [text(x0 + w / 2, y0 + h / 2, "no data", "middle")]
    lo, hi = (min(vals), max(vals)) if log_y else (0.0, max(vals))
    if log_y:
        lo, hi = 10 ** math.floor(math.log10(lo)), 10 ** math.ceil(math.log10(hi))
    else:
        hi = nice_top(hi * 1.05)

    def y(v: float) -> float:
        if log_y:
            return y0 + h - h * (math.log10(v) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
        return y0 + h - h * (v - lo) / (hi - lo)

    def x(i: int) -> float:
        return x0 + (w * i / (len(xs) - 1) if len(xs) > 1 else w / 2)

    # Axes, grid and ticks
    ticks = []
    if log_y:
        t = lo
        while t <= hi * 1.0001:
            ticks.append(t)
            t *= 10
    else:
        ticks = [hi * k / 4 for k in range(5)]
    for t in ticks:
        body.append(
            f"<line x1='{x0}' y1='{y(t):.1f}' x2='{x0 + w}' y2='{y(t):.1f}' stroke='{GRID}'/>"
        )
        body.append(text(x0 - 6, y(t) + 4, f"{t:,.3g}", "end"))
    body.append(f"<line x1='{x0}' y1='{y0 + h}' x2='{x0 + w}' y2='{y0 + h}' stroke='{INK}'/>")
    for i, name in enumerate(xs):
        body.append(text(x(i), y0 + h + 18, name, "middle"))
    body.append(
        f"<text x='{x0 - 52:.1f}' y='{y0 + h / 2:.1f}' fill='{INK}' text-anchor='middle'"
        f" transform='rotate(-90 {x0 - 52:.1f} {y0 + h / 2:.1f})'>{y_label}</text>"
    )

    # One line per series, with its points; the default variant last and heavier
    for name in sorted(series, key=lambda s: s == DEFAULT_VARIANT):
        color = VARIANT.get(name, INK)
        stroke, r = (4, 5) if name == DEFAULT_VARIANT else (2, 3.5)
        pts = [(x(i), y(v)) for i, v in enumerate(series[name]) if v is not None and v > 0]
        if len(pts) > 1:
            d = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
            body.append(
                f"<polyline points='{d}' fill='none' stroke='{color}' stroke-width='{stroke}'/>"
            )
        body += [f"<circle cx='{px:.1f}' cy='{py:.1f}' r='{r}' fill='{color}'/>" for px, py in pts]
    return body


def s8_plots(run: Path, out_dir: Path) -> None:
    """The cores compared, from an ``s8.json``."""
    doc = json.loads((run / "s8.json").read_text(encoding="utf-8"))
    data, variants = doc["data"], list(doc["variants"])
    out_dir.mkdir(parents=True, exist_ok=True)

    # Ceiling: thousand calls/s per shape, a bar per variant (the default collector setting)
    rows = [
        (
            SHAPE_NAMES.get(shape, shape),
            [
                (by["default"][v]["calls"]["median"] / 1000, variant_label(v), VARIANT.get(v, INK))
                for v in variants
                if v in by.get("default", {})
            ],
        )
        for shape, by in data["ceiling"].items()
    ]
    bars(
        "dispatcher ceiling by core: thousand node calls per second",
        rows,
        "k/s",
        out_dir / "cores-ceiling.svg",
    )

    # Scaling: us per job / item against n, one panel per shape
    shapes: dict[str, dict[int, dict]] = {}
    for key, by_v in data["scaling"].items():
        shape, n = key.rsplit(" ", 1)
        shapes.setdefault(shape, {})[int(n)] = by_v
    width, h, gap = 420, 220, 110
    total_w = 80 + len(shapes) * (width + gap)
    body = [text(10, 22, "dispatcher cost as the machine grows (lower is better)")]
    keys, below = legend(10, 48, variants, total_w - 20)
    body += keys
    top = below + 30
    for i, (shape, by_n) in enumerate(shapes.items()):
        ns = sorted(by_n)
        series = {v: [(by_n[n].get(v) or {}).get("median") for n in ns] for v in variants}
        unit = "µs per job" if shape == "resident" else "µs per item"
        title = f"{shape}: n resident jobs" if shape == "resident" else f"{shape}: n items"
        body += panel(
            80 + i * (width + gap), top, width, h, title, [str(n) for n in ns], series, unit
        )
    (out_dir / "cores-scaling.svg").write_text(svg(total_w, top + h + 50, body), encoding="utf-8")

    # GIL: engine items/s as a share of its idle rate against the dispatcher's load
    loads = doc["gil_loads"]
    configs = data["gil"]["with_work"]
    cols = 2
    width, h, gap_x, gap_y = 420, 200, 110, 90
    total_w = 80 + cols * (width + gap_x)
    body = [text(10, 22, "engine throughput under dispatcher load: share of its idle rate")]
    keys, below = legend(10, 48, variants, total_w - 20)
    body += keys
    top = below + 30
    for i, (key, by_load) in enumerate(configs.items()):
        r, c = divmod(i, cols)
        series = {
            v: [
                (
                    100 * x["share"]
                    if (x := (by_load.get(load) or {}).get(v)) and x.get("share") is not None
                    else None
                )
                for load in loads
            ]
            for v in variants
        }
        body += panel(
            80 + c * (width + gap_x),
            top + r * (h + gap_y),
            width,
            h,
            f"{key} engine",
            loads,
            series,
            "% of idle",
        )
    rows_n = math.ceil(len(configs) / cols)
    (out_dir / "cores-gil.svg").write_text(
        svg(total_w, top + rows_n * (h + gap_y), body), encoding="utf-8"
    )
    print(f"S8 plots of {run} in {out_dir}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="The README's plots")
    p.add_argument(
        "ceiling", nargs="?", default=str(ROOT / "runs" / "bench" / "dispatcher_ceiling.json")
    )
    p.add_argument("out_dir", nargs="?", default=str(ROOT / "docs" / "assets"))
    p.add_argument("--s8", default=None, help="an s8.py output folder: draw the cores compared")
    args = p.parse_args()
    if args.s8:
        s8_plots(Path(args.s8), Path(args.out_dir))
    else:
        main(Path(args.ceiling), Path(args.out_dir))
