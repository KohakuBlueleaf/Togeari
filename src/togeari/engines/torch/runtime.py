"""One placement's models, batch queue, scheduler and worker threads.

A placement is one device, or a tuple of devices a split model spans; the
models of a placement load on all of its devices (``model.load(place, dtype)``
with ``place`` the device string, or the tuple for a split). Streams and the
worker's current device are the placement's first device; memory is capped and
measured on each CUDA device of the placement.

A ``Job`` carries a payloads callable and ``reply(job, outcome)``; ``outcome``
is the per-item result list, an exception for the batch, or ``SKIPPED``. Every
job gets exactly one reply.

- ``co_resident``: all models on the device; one worker, by priority then arrival;
- ``swappable``: one model on the device at a time; its queue drains, then the
  oldest waiting model swaps in (sooner once a batch waited ``swap_quantum_ms``);
- ``priority_streams``: one worker per model on a CUDA stream of its priority.
"""

import contextlib
import itertools
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import torch

from togeari.engines.common.errors import EngineStopped

SCHEDULES = ("co_resident", "swappable", "priority_streams")


def placement(device: str | tuple | list) -> tuple[str, ...]:
    """A device or a group of devices as a tuple of device strings."""
    match device:
        case str():
            return (device,)
        case tuple() | list() if device and all(isinstance(d, str) for d in device):
            return tuple(device)
        case _:
            raise ValueError(f"a placement is a device or a non-empty tuple of devices: {device!r}")


def label(devices: tuple[str, ...]) -> str:
    """The name of a placement: its devices joined by ``+``."""
    return "+".join(devices)


class Skipped:
    """The outcome of a job cancelled before it ran."""


SKIPPED = Skipped()


@dataclass(eq=False)
class Job:
    """One batch for one model on one device."""

    id: int
    model: str
    n: int
    payloads: Callable[[], list]
    reply: Callable[["Job", Any], None]
    cancelled: bool = False
    seq: int = 0
    queued_at: float = 0.0
    batch: Any = None


@dataclass
class Slot:
    """A model on this device, with its priority and counters."""

    model: Any
    priority: int = 0
    stats: dict = field(
        default_factory=lambda: {"batches": 0, "items": 0, "busy_s": 0.0, "errors": 0}
    )


class DeviceRuntime:
    def __init__(
        self,
        device: str | tuple[str, ...],
        models: dict[str, tuple[Any, int]],
        schedule: str = "priority_streams",
        dtype: str = "bfloat16",
        memory_cap_gb: float | dict[str, float] | None = None,
        prep_workers: int = 2,
        swap_quantum_ms: float = 50.0,
    ):
        if schedule not in SCHEDULES:
            raise ValueError(f"schedule {schedule!r} not in {SCHEDULES}")

        # The placement: the name, what models load on, the first device, the CUDA devices
        self.devices = placement(device)
        self.device = label(self.devices)
        self.place: str | tuple[str, ...] = (
            self.devices[0] if len(self.devices) == 1 else self.devices
        )
        self.primary = self.devices[0]
        self.cuda = torch.device(self.primary).type == "cuda"
        self.gpus = list(dict.fromkeys(d for d in self.devices if torch.device(d).type == "cuda"))
        self.slots = {name: Slot(model, priority) for name, (model, priority) in models.items()}
        self.schedule = schedule
        self.dtype = getattr(torch, dtype)
        self.memory_cap_gb = memory_cap_gb
        self.prep_workers = prep_workers
        self.swap_quantum_s = swap_quantum_ms / 1000.0

        # Ready batches per model, guarded by ``cv``.
        self.cv = threading.Condition()
        self.ready: dict[str, deque[Job]] = {name: deque() for name in self.slots}
        self.stopping = False
        self.seq = itertools.count()
        self.resident: str | None = None
        self.swaps = {"swaps": 0, "swap_s": 0.0}
        self.footprint: dict[str, dict] = {}
        self.pool: ThreadPoolExecutor | None = None
        self.workers: list[threading.Thread] = []

    # ------------------------------------------------------------ lifecycle
    def start(self) -> dict[str, dict]:
        """Cap the allocator, load and warm every model, start the workers; returns footprints.

        Runs on the placement's first device; the calling thread's current CUDA device is the
        same afterwards."""
        with torch.cuda.device(self.primary) if self.cuda else contextlib.nullcontext():
            return self._start()

    def _start(self) -> dict[str, dict]:
        # The allocator cap on every CUDA device of the placement, before any weights load
        for gpu in self.gpus:
            cap = self.memory_cap_gb
            if isinstance(cap, dict):
                cap = cap.get(gpu)
            if cap is not None:
                total = torch.cuda.get_device_properties(gpu).total_memory
                fraction = min(1.0, cap * 2**30 / total)
                torch.cuda.set_per_process_memory_fraction(fraction, gpu)

        for name, slot in self.slots.items():
            self.footprint[name] = self._load(slot.model)

        self.pool = ThreadPoolExecutor(self.prep_workers, thread_name_prefix=f"prep-{self.device}")
        match self.schedule:
            case "priority_streams":
                plan = [(name, self._stream(slot.priority)) for name, slot in self.slots.items()]
            case _:
                plan = [(None, None)]
        for only, stream in plan:
            worker = threading.Thread(
                target=self._work,
                args=(only, stream),
                name=f"torch-{self.device}-{only or 'all'}",
                daemon=True,
            )
            worker.start()
            self.workers.append(worker)
        return self.footprint

    def _load(self, model: Any) -> dict:
        """Load, warm up and measure one model; offloaded again under ``swappable``."""
        base = self._allocated()
        for gpu in self.gpus:
            torch.cuda.reset_peak_memory_stats(gpu)

        t0 = time.perf_counter()
        with torch.inference_mode():
            model.load(self.place, self.dtype)
        self._sync(None)
        loaded = self._allocated()
        load_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        example = list(model.example())
        if example:
            with torch.inference_mode():
                model.split(model.forward(model.prepare(example)), len(example))
            self._sync(None)
        warmup_s = time.perf_counter() - t0
        peak = sum(torch.cuda.max_memory_allocated(gpu) for gpu in self.gpus)

        if self.schedule == "swappable":
            with torch.inference_mode():
                model.to("cpu")
            self._sync(None)
        return {
            "weights_mb": None if base is None else (loaded - base) / 2**20,
            "peak_mb": None if base is None else (peak - base) / 2**20,
            "load_s": load_s,
            "warmup_s": warmup_s,
            "warmup_batch": len(example),
        }

    def stop(self) -> None:
        with self.cv:
            self.stopping = True
            self.cv.notify_all()
        if self.pool is not None:
            self.pool.shutdown(wait=True)
        for worker in self.workers:
            worker.join(5.0)

        # Batches still queued are answered; the weights leave the device.
        with self.cv:
            left = [job for queue in self.ready.values() for job in queue]
            for queue in self.ready.values():
                queue.clear()
        for job in left:
            self._reply(job, EngineStopped(f"device {self.device} stopped"))
        with torch.inference_mode():
            for slot in self.slots.values():
                slot.model.to("cpu")
        if self.gpus:
            torch.cuda.empty_cache()

    # ------------------------------------------------------------ jobs
    def put(self, job: Job) -> None:
        """Queue a job for preparation; never blocks."""
        if job.model not in self.slots:
            self._reply(job, KeyError(f"no model {job.model!r} on {self.device}"))
            return
        try:
            self.pool.submit(self._prepare, job)
        except RuntimeError:
            self._reply(job, EngineStopped(f"device {self.device} stopped"))

    def _prepare(self, job: Job) -> None:
        if job.cancelled:
            self._reply(job, SKIPPED)
            return
        if self.stopping:
            self._reply(job, EngineStopped(f"device {self.device} stopped"))
            return
        try:
            with torch.inference_mode():
                job.batch = self.slots[job.model].model.prepare(job.payloads())
        except Exception as exc:  # noqa: BLE001
            self._reply(job, exc)
            return
        with self.cv:
            job.seq = next(self.seq)
            job.queued_at = time.monotonic()
            self.ready[job.model].append(job)
            self.cv.notify_all()

    def _next(self, only: str | None) -> Job | None:
        """The next job for a worker (``only``: its model); None once stopping."""
        with self.cv:
            while True:
                if self.stopping:
                    return None
                job = self._pick(only)
                if job is not None:
                    return job
                self.cv.wait()

    def _pick(self, only: str | None) -> Job | None:
        """Choose a ready job under ``cv``, by the schedule."""
        names = [only] if only is not None else [n for n, q in self.ready.items() if q]
        heads = [(n, self.ready[n][0]) for n in names if self.ready[n]]
        if not heads:
            return None
        match self.schedule:
            case "swappable":
                oldest = min(heads, key=lambda h: h[1].seq)
                stay = self.resident is not None and self.ready[self.resident]
                waited = time.monotonic() - oldest[1].queued_at
                if stay and (oldest[0] == self.resident or waited < self.swap_quantum_s):
                    name = self.resident
                else:
                    name = oldest[0]
            case _:
                name = max(heads, key=lambda h: (self.slots[h[0]].priority, -h[1].seq))[0]
        return self.ready[name].popleft()

    def _work(self, only: str | None, stream: Any) -> None:
        if self.cuda:
            torch.cuda.set_device(self.primary)
        while (job := self._next(only)) is not None:
            self._run(job, stream)

    def _run(self, job: Job, stream: Any) -> None:
        if job.cancelled:
            job.batch = None
            self._reply(job, SKIPPED)
            return
        if self.schedule == "swappable" and self.resident != job.model:
            self._swap(job.model)

        slot = self.slots[job.model]
        t0 = time.perf_counter()
        try:
            with torch.inference_mode(), self._on(stream):
                parts = list(slot.model.split(slot.model.forward(job.batch), job.n))
                self._sync(stream)
            outcome: Any = parts
        except Exception as exc:  # noqa: BLE001
            outcome = exc
            slot.stats["errors"] += 1
        slot.stats["busy_s"] += time.perf_counter() - t0
        slot.stats["batches"] += 1
        slot.stats["items"] += job.n
        job.batch = None
        self._reply(job, outcome)

    def _swap(self, name: str) -> None:
        """Offload the resident model, bring ``name`` onto the device; the time is measured."""
        t0 = time.perf_counter()
        with torch.inference_mode():
            if self.resident is not None:
                self.slots[self.resident].model.to("cpu")
            self.slots[name].model.to(self.place)
        self._sync(None)
        self.resident = name
        self.swaps["swaps"] += 1
        self.swaps["swap_s"] += time.perf_counter() - t0

    def _reply(self, job: Job, outcome: Any) -> None:
        job.payloads = _nothing
        job.reply(job, outcome)

    # ------------------------------------------------------------ device helpers
    def _stream(self, priority: int) -> Any:
        if not self.cuda:
            return None
        lowest, highest = torch.cuda.Stream.priority_range()
        level = max(highest, min(lowest, -priority))
        return torch.cuda.Stream(device=self.primary, priority=level)

    def _on(self, stream: Any) -> Any:
        return torch.cuda.stream(stream) if stream is not None else contextlib.nullcontext()

    def _sync(self, stream: Any) -> None:
        """Wait for ``stream`` (if any) and the current stream of every CUDA device."""
        if stream is not None:
            stream.synchronize()
        for gpu in self.gpus:
            torch.cuda.current_stream(gpu).synchronize()

    def _allocated(self) -> int | None:
        """Bytes allocated over the placement's CUDA devices; None without one."""
        if not self.gpus:
            return None
        self._sync(None)
        return sum(torch.cuda.memory_allocated(gpu) for gpu in self.gpus)

    def stats(self) -> dict:
        return {
            "device": self.device,
            "schedule": self.schedule,
            "resident": self.resident,
            **self.swaps,
            "models": {name: dict(slot.stats) for name, slot in self.slots.items()},
        }


def _nothing() -> list:
    return []
