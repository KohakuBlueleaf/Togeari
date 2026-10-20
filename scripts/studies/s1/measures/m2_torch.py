"""M2: dispatcher impact of torch work.

Configs: the dispatcher alone (chain load at its ceiling, and paced), then per
shape x isolation (``thread`` / ``process``) x model (``eager`` / ``graph``):
a ``TorchEngine`` with one model on ``cuda:0``, saturated either directly
(a feeder thread keeps every batch slot full) or through the machine (one job
per item on a batch lane, once per former wait in ``m2_former_waits_ms``; the
engine restarts with each machine), each alone and next to the chain load. Reports the
dispatcher's calls/s and busy fraction, the torch items/s, the engine's
counters and the allocator's VRAM.
"""

import time
from dataclasses import asdict

from s1.common import dispatch, gpu, phases
from s1.common.feeders import JobFeeder, UnitFeeder
from s1.scales import Scale, Shape
from s1.workloads import torch_net
from togeari import Lane, Use
from togeari.adapters.torch import TorchAdapter
from togeari.engines.torch import TorchEngine
from togeari.policies import SizeOrWait

NAME = "m2"
TITLE = "Dispatcher impact of torch work"


def plan(scale: Scale) -> list[dict]:
    configs = [{"name": "dispatcher-alone", "shape": None}]
    configs.extend(
        {
            "name": f"{shape}/{isolation}/{model}",
            "shape": shape,
            "isolation": isolation,
            "model": model,
        }
        for shape in scale.m2_shapes
        for isolation in scale.m2_isolations
        for model in scale.m2_models
    )
    return configs


def run(cfg: dict, scale: Scale) -> dict:
    if cfg["shape"] is None:
        return _alone(scale)
    shape = scale.shape(cfg["shape"])
    cap = scale.cap_gb(1)

    # In-process CUDA: the allocator cap comes first
    guard = gpu.guard(cap) if cfg["isolation"] == "thread" else None

    engine = TorchEngine(
        devices=[gpu.DEVICE], memory_cap_gb=cap, isolation=cfg["isolation"], name="torch"
    )
    model = torch_net.make(shape, cfg["model"])
    engine.add_model("net", model)
    unit = engine.unit("net")
    inputs = torch_net.Inputs(shape.width)

    # One machine per former wait: every phase on the first, lane phases on the rest
    rows: list[dict] = []
    start_s: list[float] = []
    for k, wait in enumerate(scale.m2_former_waits_ms):
        names = tuple(p for p in scale.m2_phases if k == 0 or phases.split(p)[0] == "lane")
        got, seconds, spec = _with_lane(engine, unit, inputs, shape, wait, names, scale)
        rows += got
        start_s.append(seconds)
    return {
        "shape": asdict(shape),
        "start_s": start_s,
        # Graph capture time, known here only when the model loaded in this process
        "capture_s": getattr(model, "capture_s", None) if guard is not None else None,
        "gpu_guard": guard,
        "engine": spec,
        "gpu_end": gpu.memory(),
        "phases": rows,
    }


def _with_lane(
    engine: TorchEngine,
    unit: object,
    inputs: torch_net.Inputs,
    shape: Shape,
    wait_ms: float,
    names: tuple[str, ...],
    scale: Scale,
) -> tuple[list[dict], float, dict]:
    """``names`` on a machine whose ``net`` lane forms batches with ``wait_ms``; lane phases
    are reported as ``<phase>@<wait>ms``. Returns the phases, the start seconds, the engine spec.
    """
    lane = Lane([unit], former=SizeOrWait(shape.max_batch, wait_ms), name="net")

    # The machine starts the engine: load, warm-up, footprint
    t0 = time.perf_counter()
    machine = dispatch.build(
        scale.chain_len,
        work=("net", ("x",)),
        libraries=(torch_net.lib,),
        bind={"net": Use(lane, adapter=TorchAdapter("x"))},
    )
    start_s = time.perf_counter() - t0

    work = {
        "direct": lambda: [UnitFeeder(unit, inputs.item, batch=shape.max_batch, name="work")],
        "lane": lambda: [
            JobFeeder(
                machine, "work", inputs.inputs, shape.max_batch * engine.slots * 2, name="work"
            )
        ],
    }
    try:
        rows = phases.run_all(
            names,
            scale,
            work,
            machine,
            probes={"engine": engine.stats, "gpu": gpu.memory},
            before=gpu.reset_peak,
            label=lambda p: f"{p}@{wait_ms:g}ms" if phases.split(p)[0] == "lane" else p,
        )
        spec = {"spec": engine.spec(), "stats": engine.stats()}
    finally:
        machine.stop()
    return rows, start_s, spec


def _alone(scale: Scale) -> dict:
    machine = dispatch.build(scale.chain_len)
    try:
        rows = phases.run_all(scale.m2_base_phases, scale, {}, machine)
    finally:
        machine.stop()
    return {"phases": rows}
