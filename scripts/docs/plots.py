"""The README's plots, as SVG files with no plotting dependency.

- ``parallel-timeline.svg``: one job of ``examples/parallel`` (each call where it ran) above
  the same calls laid one after another;
- ``parallel-speedup.svg``: the machine's time against the summed call time, one job and
  four jobs;
- ``dispatcher-ceiling.svg``: node calls per second of the ceiling shapes, from a
  ``dispatcher_ceiling.py`` report.

Usage: ``python scripts/docs/plots.py [ceiling.json] [out_dir]``.
"""

import importlib.util
import json
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
    names = {
        "chain-inline": "chain of inline nodes",
        "chain-call": "chain of lane calls",
        "each-batch": "each with batched calls",
    }
    rows = [
        (
            names[shape],
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


if __name__ == "__main__":
    main(
        (
            Path(sys.argv[1])
            if len(sys.argv) > 1
            else ROOT / "runs" / "bench" / "dispatcher_ceiling.json"
        ),
        Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs" / "assets",
    )
