"""What ``TorchEngine`` runs: the ``TorchModel`` protocol and ``TorchModule`` for a plain module."""

from collections.abc import Callable, Hashable
from typing import Any, Protocol, runtime_checkable

import torch
from torch import nn


@runtime_checkable
class TorchModel(Protocol):
    """A model the engine loads on a device and runs batches through.

    Every call except ``key`` runs on the engine's threads, under
    ``torch.inference_mode``. With ``isolation="process"`` the model object is
    pickled into the device's process before ``load``.
    """

    name: str
    max_batch: int

    def load(self, device: str | tuple[str, ...], dtype: torch.dtype) -> None:
        """Place the weights on ``device`` in ``dtype``; a tuple of devices for a model split
        across them."""

    def to(self, device: str | tuple[str, ...]) -> None:
        """Move the loaded weights (``"cpu"`` offloads them; swappable scheduling); the
        tuple it was loaded on brings a split model back."""

    def example(self) -> list[Any]:
        """Payloads for the warm-up batch at load; empty for no warm-up."""

    def prepare(self, payloads: list[Any]) -> Any:
        """CPU side: build the batch (stack, pad, pin)."""

    def forward(self, batch: Any) -> Any:
        """Device side: move the batch to the device and run it."""

    def split(self, out: Any, n: int) -> list[Any]:
        """Per-item results, in input order."""

    def key(self, payload: Any) -> Hashable:
        """Shape bucket: payloads with equal keys may share a batch."""


def stack(payloads: list[Any]) -> Any:
    """Stack tensor payloads, or dicts of tensors key by key."""
    match payloads[0]:
        case dict():
            return {k: torch.stack([p[k] for p in payloads]) for k in payloads[0]}
        case _:
            return torch.stack(payloads)


def rows(out: Any, n: int) -> list[Any]:
    """Row ``i`` of the output (or of each output in a tuple / dict) for item ``i``."""
    match out:
        case dict():
            return [{k: v[i] for k, v in out.items()} for i in range(n)]
        case tuple() | list():
            return [tuple(v[i] for v in out) for i in range(n)]
        case _:
            return [out[i] for i in range(n)]


def shape_key(payload: Any) -> Hashable:
    """Shapes and dtypes of a tensor payload, or of each tensor in a dict payload."""
    match payload:
        case torch.Tensor():
            return (tuple(payload.shape), payload.dtype)
        case dict():
            return tuple(sorted((k, shape_key(v)) for k, v in payload.items()))
        case _:
            return type(payload).__qualname__


def place(batch: Any, device: str, dtype: torch.dtype) -> Any:
    """Move tensors to ``device``; floating tensors also take ``dtype``. Copies to a CUDA
    device do not block; copies to the host do, so the result is ready when returned."""
    match batch:
        case torch.Tensor() if batch.is_floating_point():
            async_ = torch.device(device).type == "cuda"
            return batch.to(device=device, dtype=dtype, non_blocking=async_)
        case torch.Tensor():
            async_ = torch.device(device).type == "cuda"
            return batch.to(device=device, non_blocking=async_)
        case dict():
            return {k: place(v, device, dtype) for k, v in batch.items()}
        case tuple() | list():
            return type(batch)(place(v, device, dtype) for v in batch)
        case _:
            return batch


def stage_plan(sizes: list[int], n: int) -> list[int]:
    """Device index per child: contiguous runs with near-equal parameter counts over ``n``
    devices, a child without parameters on its predecessor's device (equal child counts when
    no child has parameters)."""
    total = sum(sizes)
    if total == 0:
        sizes, total = [1] * len(sizes), len(sizes)
    plan, before = [], 0
    for size in sizes:
        match size:
            case 0 if plan:
                plan.append(plan[-1])
            case _:
                plan.append(min(n - 1, int((before + size / 2) * n / total)))
        before += size
    return plan


class TorchModule:
    """A plain ``nn.Module`` as a ``TorchModel``.

    ``collate(payloads)`` builds the CPU batch (default: stack), pinned when the
    first device is CUDA; ``forward`` places it on the device and calls the
    module with it (a dict batch is passed as keywords); ``split(out, n)`` gives
    the per-item results (default: row ``i``); ``key(payload)`` the shape bucket
    (default: shapes and dtypes).

    Loaded on a tuple of devices, an ``nn.Sequential`` is split into stages: its
    children in order, each on the device ``stages[i]`` indexes in the tuple
    (default: ``stage_plan``, contiguous runs of near-equal parameter count).
    ``forward`` runs the stages in order and moves the activations to each
    stage's device; the output stays on the last stage's device. ``to("cpu")``
    offloads every stage; ``to`` with the tuple places the stages again.
    """

    def __init__(
        self,
        module: nn.Module,
        collate: Callable[[list[Any]], Any] = stack,
        split: Callable[[Any, int], list[Any]] = rows,
        max_batch: int = 32,
        key: Callable[[Any], Hashable] = shape_key,
        example: list[Any] | tuple = (),
        name: str | None = None,
        stages: list[int] | None = None,
    ):
        self.module = module
        self.collate = collate
        self.splitter = split
        self.max_batch = max_batch
        self.keyer = key
        self.examples = list(example)
        self.name = name or type(module).__name__
        self.stages = stages
        self.device = "cpu"
        self.dtype = torch.float32
        # (device, stage module) per stage while split; None while on one device
        self.parts: list[tuple[str, nn.Module]] | None = None

    def load(self, device: str | tuple[str, ...], dtype: torch.dtype) -> None:
        self.dtype = dtype
        match device:
            case str():
                self.parts = None
                self.device = device
                self.module.to(device=device, dtype=dtype)
            case _:
                self.module.to(dtype=dtype)
                self.to(device)
        self.module.eval()
        self.module.requires_grad_(False)

    def to(self, device: str | tuple[str, ...]) -> None:
        match device:
            case str():
                self.module.to(device)
                if self.parts is None:
                    self.device = device
            case _:
                self.parts = self._split(tuple(device))
                self.device = self.parts[0][0]
                for dev, part in self.parts:
                    part.to(dev)

    def _split(self, devices: tuple[str, ...]) -> list[tuple[str, nn.Module]]:
        """The stages over ``devices``: runs of consecutive children on one device."""
        if not isinstance(self.module, nn.Sequential):
            raise TypeError(f"{self.name}: a split needs an nn.Sequential, not {type(self.module)}")
        children = list(self.module)
        plan = self.stages
        if plan is None:
            plan = stage_plan(
                [sum(p.numel() for p in c.parameters()) for c in children], len(devices)
            )
        if len(plan) != len(children) or not all(0 <= i < len(devices) for i in plan):
            raise ValueError(
                f"{self.name}: stages {plan} do not fit {len(children)} children on {devices}"
            )

        # Consecutive children on one device form one stage
        parts: list[tuple[str, list]] = []
        for child, index in zip(children, plan):
            if parts and parts[-1][0] == index:
                parts[-1][1].append(child)
            else:
                parts.append((index, [child]))
        return [(devices[i], nn.Sequential(*chunk)) for i, chunk in parts]

    def example(self) -> list[Any]:
        return self.examples

    def prepare(self, payloads: list[Any]) -> Any:
        batch = self.collate(payloads)
        if torch.device(self.device).type == "cuda":
            batch = _pin(batch)
        return batch

    def forward(self, batch: Any) -> Any:
        if self.parts is None:
            return _call(self.module, place(batch, self.device, self.dtype))

        # Stages in order, the activations moved to each stage's device
        out = batch
        for dev, part in self.parts:
            out = _call(part, place(out, dev, self.dtype))
        return out

    def split(self, out: Any, n: int) -> list[Any]:
        return self.splitter(out, n)

    def key(self, payload: Any) -> Hashable:
        return self.keyer(payload)


def _call(module: nn.Module, batch: Any) -> Any:
    """``module`` on ``batch``; a dict batch is passed as keywords."""
    match batch:
        case dict():
            return module(**batch)
        case _:
            return module(batch)


def _pin(batch: Any) -> Any:
    match batch:
        case torch.Tensor() if batch.device.type == "cpu":
            return batch.pin_memory()
        case dict():
            return {k: _pin(v) for k, v in batch.items()}
        case _:
            return batch
