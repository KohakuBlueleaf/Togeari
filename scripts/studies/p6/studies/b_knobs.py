"""Study B: the knob defaults, one knob varied at a time from the machine's defaults.

Workloads: the three ceiling shapes and ``examples/trace_replay`` (latency samples), each with
``Counters`` attached so event sampling has an effect. Settings: the defaults (``window`` 64,
``retire_buffer`` 4096, ``event_sample`` 0.1, ``Fixed(64)``), then ``window`` 16 / 256,
``retire_buffer`` 1024, ``event_sample`` 0.0 / 1.0, ``Fixed(16)`` / ``Fixed(256)`` and
``TargetLoad(waiting=24)`` on the workload's first engine lane (``trace_replay``'s own
admission); interleaved over rounds. Every run also
records the inline nodes' run times, the input for ``inline_warn_us``.
"""

from pathlib import Path

from p6.common import measure
from p6.common.examples import ceiling, ceiling_jobs, example
from p6.common.report import write_summary
from p6.scales import Scale
from togeari import Machine
from togeari.core.construct.knobs import checked
from togeari.io import Iterable, Memory
from togeari.plugins import Counters
from togeari.policies import Fixed, TargetLoad

TITLE = "Knob defaults: one knob at a time from the defaults"
WORKLOADS = (*ceiling.SHAPES, "trace_replay")

# (name, knob overrides); "admission" values are built per machine
SETTINGS = (
    ("defaults", {}),
    ("window 16", {"window": 16}),
    ("window 256", {"window": 256}),
    ("retire_buffer 1024", {"retire_buffer": 1024}),
    ("event_sample 0.0", {"event_sample": 0.0}),
    ("event_sample 1.0", {"event_sample": 1.0}),
    ("Fixed(16)", {"admission": ("fixed", 16)}),
    ("Fixed(256)", {"admission": ("fixed", 256)}),
    ("TargetLoad", {"admission": ("target", 24)}),
)


def plan(scale: Scale) -> list[dict]:
    cfgs = []
    for r in range(scale.rounds):
        for workload in WORKLOADS:
            for setting, knobs in SETTINGS:
                # TargetLoad needs an engine lane; the inline chain has none
                if setting == "TargetLoad" and workload == "chain-inline":
                    continue
                cfgs.append(
                    {
                        "name": f"{workload} {setting}",
                        "round": r,
                        "workload": workload,
                        "setting": setting,
                        "knobs": knobs,
                    }
                )
    return cfgs


def admission(spec: tuple, machine: Machine):
    match spec:
        case ("fixed", n):
            return Fixed(n)
        case ("target", waiting):
            return TargetLoad(machine.lanes[0], waiting=waiting)


def apply(machine: Machine, knobs: dict) -> None:
    """Set the overrides on a built machine, each through the knob check."""
    for name, value in {"admission": ("fixed", 64), **knobs}.items():
        if name == "admission":
            value = admission(value, machine)
        value, problem = checked(name, value, machine.contributed_policies)
        if problem is not None:
            raise ValueError(problem)
        machine.knobs[name] = value


def build(cfg: dict, scale: Scale) -> tuple:
    match cfg["workload"]:
        case "trace_replay":
            machine, source, sink = example("trace_replay").build()
            machine.command("plugin_add", plugin=Counters()).result(30)
        case shape:
            program, bind, _ = ceiling.SHAPES[shape]
            machine = Machine(
                program=program,
                entry="main",
                libraries=[ceiling.lib],
                bind=bind(),
                plugins=[Counters()],
            )
            source, sink = Iterable(ceiling_jobs(shape, scale.ceiling_share)), Memory()
    return machine, source, sink


def run(cfg: dict, scale: Scale, root: Path) -> dict:
    # The run's base knobs first, then the setting under study
    machine, source, sink = build(cfg, scale)
    apply(machine, {**cfg.get("base_knobs", {}), **cfg["knobs"]})
    return measure.run(machine, source, sink)


def line(cfg: dict, res: dict) -> str:
    return (
        f"{res['calls_per_s']:9.0f} calls/s  {res['jobs_per_s']:8.1f} jobs/s"
        f"  peak jobs {res['peak_jobs']:5d}  peak bytes {res['peak_pool_bytes']:>10,}"
    )


def summarize(results: list[dict], root: Path) -> Path:
    """Per workload and setting: calls/s and jobs/s spreads, peaks; then inline node times."""
    by_name: dict[str, list[dict]] = {}
    for r in results:
        if "error" not in r:
            by_name.setdefault(r["config"]["name"], []).append(r["result"])
    rows = []
    inline: dict[str, list[float]] = {}
    for name, runs in by_name.items():
        rate = measure.spread([x["calls_per_s"] for x in runs])
        jobs = measure.spread([x["jobs_per_s"] for x in runs])
        rows.append(
            [
                name,
                rate["median"],
                f"{rate['best']:,.0f}-{rate['worst']:,.0f}",
                jobs["median"],
                max(x["peak_jobs"] for x in runs),
                max(x["peak_pool_bytes"] for x in runs),
                rate["rounds"],
            ]
        )
        for x in runs:
            for node, p in x["inline"].items():
                inline.setdefault(node, []).append(p.get("p50_us", 0.0))

    header = [
        "workload / setting",
        "calls/s median",
        "best-worst",
        "jobs/s median",
        "peak jobs",
        "peak pool bytes",
        "rounds",
    ]
    times = "; ".join(f"`{n}` p50 {min(v):.1f}-{max(v):.1f} us" for n, v in sorted(inline.items()))
    notes = (
        f"Inline node run times over every run (per-run p50 range): {times}. "
        "Per-run p90 / p99 / max are in the configuration files."
    )
    return write_summary(root, TITLE, header, rows, notes)
