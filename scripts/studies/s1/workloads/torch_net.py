"""A residual MLP of configurable depth and width, eager or replayed from CUDA graphs.

Both are ``TorchModule``s (importable here, so device processes can unpickle
them). Results come back to the host in ``split``, as float32 rows.
``GraphModule`` captures one CUDA graph per batch-size bucket (powers of two
up to ``max_batch``) at load and replays the smallest bucket that fits.
"""

from typing import Any

import torch
from kohakunode import NodeLibrary
from torch import nn

from s1.scales import Shape
from togeari.adapters.torch import TorchAdapter
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.torch import TorchModule

lib = NodeLibrary("s1torch")


@lib.node()
def net(x: Any) -> Any:
    """The signature of the ``net`` node; its calls run on a ``TorchEngine`` unit."""
    raise NotImplementedError("net runs on a TorchEngine unit")


class Block(nn.Module):
    def __init__(self, width: int, dtype: torch.dtype):
        super().__init__()
        self.norm = nn.LayerNorm(width, dtype=dtype)
        self.proj = nn.Linear(width, width, dtype=dtype)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + torch.nn.functional.gelu(self.proj(self.norm(x)))


class Net(nn.Module):
    def __init__(self, width: int, depth: int, out: int, dtype: torch.dtype = torch.float32):
        super().__init__()
        self.blocks = nn.Sequential(*[Block(width, dtype) for _ in range(depth)])
        self.head = nn.Linear(width, out, dtype=dtype)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(self.blocks(x))


def host_rows(out: Any, n: int) -> list:
    """Row ``i`` of the output, on the host in float32."""
    return list(out.float().cpu().unbind(0))[:n]


def buckets(max_batch: int) -> list[int]:
    """Powers of two below ``max_batch``, and ``max_batch`` itself."""
    sizes = []
    b = 1
    while b < max_batch:
        sizes.append(b)
        b *= 2
    return [*sizes, max_batch]


class GraphModule(TorchModule):
    """A ``TorchModule`` whose forward replays a captured CUDA graph per batch bucket."""

    def __init__(self, module: nn.Module, width: int, **kw: Any):
        super().__init__(module, **kw)
        self.width = width
        self.graphs: dict[int, tuple] = {}
        self.capture_s = 0.0

    def __getstate__(self) -> dict:
        state = dict(self.__dict__)
        state["graphs"] = {}
        return state

    def load(self, device: str, dtype: torch.dtype) -> None:
        super().load(device, dtype)
        self._capture()

    def to(self, device: str) -> None:
        self.graphs = {}
        super().to(device)
        if torch.device(device).type == "cuda":
            self._capture()

    def _capture(self) -> None:
        """One graph per bucket, largest first, all in one memory pool."""
        t0 = torch.cuda.Event(enable_timing=True)
        t1 = torch.cuda.Event(enable_timing=True)
        t0.record()
        pool = None
        for b in sorted(buckets(self.max_batch), reverse=True):
            x = torch.zeros(b, self.width, device=self.device, dtype=self.dtype)

            # Warm-up on a side stream, then capture
            side = torch.cuda.Stream(self.device)
            side.wait_stream(torch.cuda.current_stream(self.device))
            with torch.cuda.stream(side):
                for _ in range(3):
                    self.module(x)
            torch.cuda.current_stream(self.device).wait_stream(side)
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph, pool=pool):
                y = self.module(x)
            pool = graph.pool()
            self.graphs[b] = (graph, x, y)
        t1.record()
        t1.synchronize()
        self.capture_s = t0.elapsed_time(t1) / 1000.0

    def forward(self, batch: Any) -> Any:
        n = batch.shape[0]
        b = next(s for s in sorted(self.graphs) if s >= n)
        graph, x, y = self.graphs[b]
        x[:n].copy_(batch, non_blocking=True)
        graph.replay()
        return y[:n].clone()


def make(shape: Shape, kind: str, seed: int = 0) -> TorchModule:
    """The ``shape`` module as ``kind`` ``"eager"`` or ``"graph"``, warmed on a full batch."""
    torch.manual_seed(seed)
    # Host weights in bf16: half the init time and half the bytes into a device process
    module = Net(shape.width, shape.depth, shape.out, dtype=torch.bfloat16)
    kw = {
        "split": host_rows,
        "max_batch": shape.max_batch,
        "example": [torch.zeros(shape.width)] * shape.max_batch,
        "name": f"{shape.name}-{kind}",
    }
    match kind:
        case "eager":
            return TorchModule(module, **kw)
        case "graph":
            return GraphModule(module, shape.width, **kw)
        case _:
            raise ValueError(f"model kind {kind!r}")


class Inputs:
    """A pool of ``pool`` random host tensors of ``width``, handed out round-robin."""

    def __init__(self, width: int, pool: int = 64, seed: int = 0):
        g = torch.Generator().manual_seed(seed)
        self.tensors = [torch.randn(width, generator=g) for _ in range(pool)]

    def item(self, i: int) -> WorkItem:
        """A work item for a ``net`` call, as the lane would build it."""
        return WorkItem(
            "net", {"x": self.tensors[i % len(self.tensors)]}, TorchAdapter("x"), "torch"
        )

    def inputs(self, i: int) -> dict:
        """Job inputs for the ``work`` entry."""
        return {"x": self.tensors[i % len(self.tensors)]}
