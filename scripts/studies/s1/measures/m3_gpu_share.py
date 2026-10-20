"""M3: two models sharing ``cuda:0``.

Layouts ``<isolation>/<arrangement>``: one engine holding both models
(``co_resident``, ``swappable``, ``priority_streams``) or one engine per model
(``two-engines``), with ``thread`` isolation (one process) or ``process``
isolation (device processes). Phases: model ``a`` alone, ``b`` alone, ``both``
at once, each saturated by a feeder thread. Interference is the ``both``
throughput over the alone throughput; VRAM comes from the allocator here, the
engines' footprints, and the parent's device samples.
"""

import time
from dataclasses import asdict

from s1.common import gpu, phases
from s1.common.feeders import UnitFeeder
from s1.scales import Scale
from s1.workloads import torch_net
from togeari.engines.torch import TorchEngine

NAME = "m3"
TITLE = "GPU sharing between two models"


def plan(scale: Scale) -> list[dict]:
    a, b = scale.m3_models
    configs = []
    for layout in scale.m3_layouts:
        isolation, arrangement = layout.split("/")
        configs.append(
            {
                "name": layout,
                "isolation": isolation,
                "arrangement": arrangement,
                "models": {"a": a, "b": b},
            }
        )
    return configs


def engines_for(cfg: dict, scale: Scale) -> dict[str, TorchEngine]:
    """Model role ("a" / "b") -> its engine; one shared engine unless ``two-engines``."""
    two = cfg["arrangement"] == "two-engines"
    processes = 2 if two and cfg["isolation"] == "process" else 1
    cap = scale.cap_gb(processes)

    # One engine per model
    if two:
        return {
            role: TorchEngine(
                devices=[gpu.DEVICE],
                memory_cap_gb=cap,
                isolation=cfg["isolation"],
                name=f"torch-{role}",
            )
            for role in cfg["models"]
        }

    # One engine for both, the arrangement as its schedule
    shared = TorchEngine(
        devices=[gpu.DEVICE],
        memory_cap_gb=cap,
        isolation=cfg["isolation"],
        schedule=cfg["arrangement"],
        name="torch",
    )
    return {role: shared for role in cfg["models"]}


def run(cfg: dict, scale: Scale) -> dict:
    in_process = cfg["isolation"] == "thread"
    guard = gpu.guard(scale.cap_gb(1)) if in_process else None
    engines = engines_for(cfg, scale)
    unique = list({id(e): e for e in engines.values()}.values())

    # Models in, then every engine starts (load, warm-up, footprint)
    shapes = {role: scale.shape(name) for role, name in cfg["models"].items()}
    inputs = {role: torch_net.Inputs(s.width) for role, s in shapes.items()}
    for role, engine in engines.items():
        engine.add_model(role, torch_net.make(shapes[role], "eager", seed=ord(role)))
    t0 = time.perf_counter()
    for engine in unique:
        engine.start()
    start_s = time.perf_counter() - t0

    def feeder(role: str) -> UnitFeeder:
        unit = engines[role].unit(role)
        return UnitFeeder(unit, inputs[role].item, batch=shapes[role].max_batch, name=role)

    work = {
        "a": lambda: [feeder("a")],
        "b": lambda: [feeder("b")],
        "both": lambda: [feeder("a"), feeder("b")],
    }
    probes = {e.name: e.stats for e in unique}
    probes["gpu"] = gpu.memory
    try:
        rows = phases.run_all(("a", "b", "both"), scale, work, probes=probes, before=gpu.reset_peak)
        specs = {e.name: {"spec": e.spec(), "stats": e.stats()} for e in unique}
    finally:
        for engine in unique:
            engine.stop()
    return {
        "shapes": {role: asdict(s) for role, s in shapes.items()},
        "start_s": start_s,
        "gpu_guard": guard,
        "engines": specs,
        "gpu_end": gpu.memory(),
        "phases": rows,
        "interference": interference(rows),
    }


def interference(rows: list[dict]) -> dict:
    """Per model: items/s with both running over items/s alone."""
    by = {r["phase"]: r for r in rows if r.get("error") is None}
    out = {}
    for role in ("a", "b"):
        alone = by.get(role, {}).get("feeders", {}).get(role, {}).get("items_per_s")
        both = by.get("both", {}).get("feeders", {}).get(role, {}).get("items_per_s")
        out[role] = {
            "alone_items_per_s": alone,
            "both_items_per_s": both,
            "ratio": both / alone if alone and both is not None else None,
        }
    return out
