"""The engine work of one config: a torch network on ``cuda:0``, or a GIL-holding Python loop.

``build(work, isolation, scale)`` returns a ``Work``: the engine (the unit fed directly), the
item maker and batch size of its feeder, and what the machine binds so it starts the engine.
"""

from dataclasses import dataclass
from typing import Any

from s1.common import gpu
from s1.workloads import pyfuncs, torch_net

from p8.scales import Scale
from togeari import Lane, Use
from togeari.adapters.torch import TorchAdapter
from togeari.core.contracts.adapter import PythonAdapter
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.python import PythonCallEngine
from togeari.engines.torch import TorchEngine
from togeari.policies import SizeOrWait


@dataclass
class Work:
    engine: Any
    unit: Any
    make: Any
    batch: int | None
    node: tuple[str, tuple[str, ...]]
    libraries: tuple
    bind: dict
    guard: dict | None
    probe: Any


def build(work: str, isolation: str, scale: Scale) -> Work:
    match work:
        case "torch":
            return _torch(isolation, scale)
        case "python":
            return _python(isolation, scale)
    raise ValueError(f"work {work!r}")


def _torch(isolation: str, scale: Scale) -> Work:
    # In-process CUDA: the allocator cap comes first
    guard = gpu.guard(scale.vram_cap_gb) if isolation == "thread" else None
    engine = TorchEngine(
        devices=[gpu.DEVICE], memory_cap_gb=scale.vram_cap_gb, isolation=isolation, name="torch"
    )
    engine.add_model("net", torch_net.make(scale.shape, "eager"))
    unit = engine.unit("net")
    inputs = torch_net.Inputs(scale.shape.width)
    lane = Lane([unit], former=SizeOrWait(scale.shape.max_batch, 2.0), name="net")
    return Work(
        engine=engine,
        unit=unit,
        make=inputs.item,
        batch=scale.shape.max_batch,
        node=("net", ("x",)),
        libraries=(torch_net.lib,),
        bind={"net": Use(lane, adapter=TorchAdapter("x"))},
        guard=guard,
        probe=lambda: {"engine": engine.stats(), "gpu": gpu.memory()},
    )


def _python(isolation: str, scale: Scale) -> Work:
    engine = PythonCallEngine(
        workers=scale.py_workers,
        mode=isolation,
        transport="pickle",
        imports=(pyfuncs.__name__,),
        name="py",
    )
    adapter = PythonAdapter(pyfuncs.spin)

    def make(i: int) -> WorkItem:
        return WorkItem("spin", {"x": i, "n": scale.spin_n}, adapter, "python")

    return Work(
        engine=engine,
        unit=engine,
        make=make,
        batch=None,
        node=("spin", pyfuncs.INPUTS["spin"]),
        libraries=(pyfuncs.lib,),
        bind={"spin": Use(engine)},
        guard=None,
        probe=lambda: {"engine": engine.load().extra},
    )
