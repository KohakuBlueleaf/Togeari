"""Tiny synthetic models and items for the TorchEngine checks (importable by device processes)."""

import os
import threading
import time

import torch
from torch import nn

from togeari.adapters.torch import TorchAdapter
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.torch import TorchModule

FEATURES = 16
# Inputs above this make ``Hang`` hang.
HANG_AT = 100.0


class Sleepy(nn.Module):
    """Doubles its input after a pause, so a batch is still running when its process is killed."""

    def __init__(self, pause_s: float = 0.5):
        super().__init__()
        self.pause_s = pause_s
        self.scale = nn.Parameter(torch.full((FEATURES,), 2.0))

    def forward(self, x):
        time.sleep(self.pause_s)
        return x * self.scale


class Hang(nn.Module):
    """Doubles its input; an input above ``HANG_AT`` first hangs for ``pause_s``."""

    def __init__(self, pause_s: float):
        super().__init__()
        self.pause_s = pause_s
        self.scale = nn.Parameter(torch.full((FEATURES,), 2.0))

    def forward(self, x):
        if bool((x > HANG_AT).any()):
            time.sleep(self.pause_s)
        return x * self.scale


class Gated(TorchModule):
    """A linear model whose ``load`` waits for ``gate`` (thread isolation only), then raises
    ``error`` when one is set."""

    def __init__(self):
        torch.manual_seed(0)
        super().__init__(nn.Linear(FEATURES, 8), max_batch=8, name="gated")
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.error: BaseException | None = None

    def load(self, device, dtype):
        self.entered.set()
        self.gate.wait(60.0)
        if self.error is not None:
            raise self.error
        super().load(device, dtype)


class Flaky(TorchModule):
    """A linear model whose ``load`` fails while the file ``flag`` exists."""

    def __init__(self, flag: str):
        torch.manual_seed(0)
        super().__init__(nn.Linear(FEATURES, 8), max_batch=8, name="flaky")
        self.flag = flag

    def load(self, device, dtype):
        if os.path.exists(self.flag):
            raise RuntimeError(f"{self.flag} exists")
        super().load(device, dtype)


class CapProbe(TorchModule):
    """A linear model that records the device's memory fraction when its ``load`` starts."""

    def __init__(self):
        torch.manual_seed(0)
        super().__init__(nn.Linear(FEATURES, 8), max_batch=8, name="cap_probe")
        self.fraction_at_load: float | None = None

    def load(self, device, dtype):
        index = torch.device(device).index or 0
        self.fraction_at_load = torch.cuda.get_per_process_memory_fraction(index)
        super().load(device, dtype)


def hang(pause_s: float = 600.0) -> TorchModule:
    return TorchModule(Hang(pause_s), max_batch=4, example=[torch.zeros(FEATURES)], name="hang")


def hang_items(n: int = 2) -> list[WorkItem]:
    """Items whose batch hangs."""
    return [
        WorkItem("hang", {"x": torch.full((FEATURES,), HANG_AT * 10)}, TorchAdapter("x"), "torch")
        for _ in range(n)
    ]


def linear(seed: int = 0, out: int = 8) -> TorchModule:
    torch.manual_seed(seed)
    module = nn.Linear(FEATURES, out)
    return TorchModule(module, max_batch=8, example=[torch.zeros(FEATURES)] * 2, name=f"lin{seed}")


def sleepy() -> TorchModule:
    return TorchModule(Sleepy(), max_batch=4, example=[torch.zeros(FEATURES)], name="sleepy")


def items(node: str, n: int = 6, seed: int = 1) -> list[WorkItem]:
    g = torch.Generator().manual_seed(seed)
    return [
        WorkItem(node, {"x": torch.randn(FEATURES, generator=g)}, TorchAdapter("x"), "torch")
        for _ in range(n)
    ]


def bad_item(node: str) -> WorkItem:
    """A payload of the wrong width: its forward raises."""
    return WorkItem(node, {"x": torch.randn(FEATURES - 3)}, TorchAdapter("x"), "torch")
