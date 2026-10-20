"""Study A: per-call dispatcher cost by event level and observing plugins.

CPU: the three ceiling shapes, every level (``all``, ``sampled``, ``counters``) with no
plugin, ``Counters`` or ``RunReport``, interleaved over rounds. GPU: ``examples/mixed`` with the
same grid (``record`` off, so the level alone decides the detail events), plus the example's
own setting (``sampled``, ``RunReport``, ``record=1.0``).
"""

from pathlib import Path

from p6.common import measure
from p6.common.examples import ceiling, ceiling_jobs, example
from p6.common.report import slug, write_summary
from p6.scales import Scale
from togeari import Machine
from togeari.io import Iterable, Memory
from togeari.plugins import Counters, RunReport
from togeari.policies import Fixed

TITLE = "Events cost: dispatcher time per call by event level and plugins"
LEVELS = ("all", "sampled", "counters")
PLUGINS = ("none", "counters", "report")


def plugins_for(kind: str, folder: Path) -> list:
    match kind:
        case "none":
            return []
        case "counters":
            return [Counters()]
        case "report":
            return [RunReport(folder)]


def plan(scale: Scale) -> list[dict]:
    """Configurations in run order: CPU rounds interleaved, then the GPU grid."""
    cfgs = []
    for r in range(scale.rounds):
        for shape in ceiling.SHAPES:
            for level in LEVELS:
                for kind in PLUGINS:
                    name = f"cpu {shape} {level} {kind}"
                    cfgs.append(
                        {
                            "name": name,
                            "round": r,
                            "where": "cpu",
                            "shape": shape,
                            "events": level,
                            "plugins": kind,
                        }
                    )
    for r in range(scale.gpu_rounds):
        cfgs.extend(
            {
                "name": f"mixed {level} {kind}",
                "round": r,
                "where": "mixed",
                "events": level,
                "plugins": kind,
                "record": None,
            }
            for level in LEVELS
            for kind in PLUGINS
        )
        cfgs.append(
            {
                "name": "mixed example-default",
                "round": r,
                "where": "mixed",
                "events": "sampled",
                "plugins": "report",
                "record": 1.0,
            }
        )
    return cfgs


def run(cfg: dict, scale: Scale, root: Path) -> dict:
    folder = root / "reports" / f"{slug(cfg['name'])}-r{cfg['round']}"
    match cfg["where"]:
        case "cpu":
            program, bind, _ = ceiling.SHAPES[cfg["shape"]]
            machine = Machine(
                program=program,
                entry="main",
                libraries=[ceiling.lib],
                bind=bind(),
                admission=Fixed(256),
                plugins=plugins_for(cfg["plugins"], folder),
                events=cfg["events"],
            )
            jobs = ceiling_jobs(cfg["shape"], scale.ceiling_share)
            return measure.run(machine, Iterable(jobs), Memory())
        case "mixed":
            mixed = example("mixed")
            mixed.nodes.LOG["path"] = root / "logs" / f"{slug(cfg['name'])}.log"
            mixed.nodes.LOG["path"].parent.mkdir(parents=True, exist_ok=True)
            machine, source, sink = mixed.build(
                stations=scale.mixed_stations,
                report=folder if cfg["plugins"] == "report" else None,
                plugins=[Counters()] if cfg["plugins"] == "counters" else [],
                events=cfg["events"],
                record=cfg["record"],
            )
            return measure.run(machine, source, sink)


def line(cfg: dict, res: dict) -> str:
    return (
        f"{res['calls_per_s']:9.0f} calls/s  dispatcher {res['dispatcher_us_per_call']:6.1f}"
        f" us/call  busy {res['dispatcher_busy']:.2f}"
    )


def summarize(results: list[dict], root: Path) -> Path:
    """Per configuration name: median (best-worst) calls/s and dispatcher us per call."""
    by_name: dict[str, list[dict]] = {}
    for r in results:
        if "error" not in r:
            by_name.setdefault(r["config"]["name"], []).append(r["result"])
    rows = []
    for name, runs in by_name.items():
        rate = measure.spread([x["calls_per_s"] for x in runs])
        cost = measure.spread([x["dispatcher_us_per_call"] for x in runs])
        rows.append(
            [
                name,
                rate["median"],
                f"{rate['best']:,.0f}-{rate['worst']:,.0f}",
                cost["median"],
                runs[0]["dispatcher_busy"],
                rate["rounds"],
            ]
        )
    header = ["config", "calls/s median", "best-worst", "dispatcher us/call", "busy", "rounds"]
    notes = (
        "Dispatcher us/call is the dispatcher's busy time divided by node calls. "
        "`mixed` rows have `record` off except `example-default`."
    )
    return write_summary(root, TITLE, header, rows, notes)
