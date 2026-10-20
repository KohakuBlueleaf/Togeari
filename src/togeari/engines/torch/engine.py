"""``TorchEngine``: devices, the models on them, and one batch unit per (model, placement).

A placement is one device, or a tuple of devices one split model spans (``runtime.placement``);
it is named by ``runtime.label`` (``"cuda:0"``, ``"cuda:0+cuda:1"``).
"""

import copy
import threading
from collections.abc import Hashable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import torch

from togeari.core.contracts.engine import Load
from togeari.core.contracts.results import Cancelled, ItemError, UnitError
from togeari.engines.common.errors import EngineStopped
from togeari.engines.common.state import EngineState
from togeari.engines.common.ticket import Ticket, finish
from togeari.engines.common.transport import SHM_THRESHOLD
from togeari.engines.torch.backends import LocalDevice, RemoteDevice
from togeari.engines.torch.runtime import SCHEDULES, Job, Skipped, label, placement


@dataclass
class _Entry:
    model: Any
    priority: int
    placements: list[tuple[str, ...]]
    # Deep copies for every placement after the first, made once (thread isolation)
    copies: dict[tuple[str, ...], Any] = field(default_factory=dict)

    @property
    def devices(self) -> list[str]:
        """The placements' names."""
        return [label(p) for p in self.placements]

    def instance(self, p: tuple[str, ...], shared_process: bool) -> Any:
        """The model object placement ``p`` loads: the registered model for the first
        placement, or always under process isolation (each process unpickles its own); a
        deep copy of it, made once before anything loads, for every other placement in one
        process."""
        if not shared_process or p == self.placements[0]:
            return self.model
        if p not in self.copies:
            self.copies[p] = copy.deepcopy(self.model)
        return self.copies[p]


class _Start:
    """One start in progress: ``settled`` is set when it ends, ``error`` its failure."""

    def __init__(self):
        self.settled = threading.Event()
        self.error: BaseException | None = None

    def finish(self, error: BaseException | None) -> None:
        self.error = error
        self.settled.set()

    def wait(self) -> None:
        self.settled.wait()
        if self.error is not None:
            raise self.error


def _payloads(items: list) -> list:
    return [item.payload() for item in items]


def _results(items: list, outcome: Any) -> list:
    """Per-item results from a device outcome."""
    match outcome:
        case list() if len(outcome) == len(items):
            return [finish(item, raw) for item, raw in zip(items, outcome)]
        case list():
            err = RuntimeError(f"split gave {len(outcome)} results for {len(items)} items")
            return [ItemError(err)] * len(items)
        case Skipped():
            return [Cancelled()] * len(items)
        case EngineStopped() | torch.OutOfMemoryError():
            return [UnitError(outcome)] * len(items)
        case _:
            return [ItemError(outcome)] * len(items)


class TorchEngine:
    """Owns ``devices`` and the models on them, and schedules their batches.

    ``add_model(name, model, devices=...)`` registers a ``TorchModel``; each entry
    of ``devices`` is a device (one unit per device) or a tuple of devices the
    model is split across (one unit for the tuple); without ``devices`` the model
    is on the engine's first device. ``unit(name)`` is that model's batch unit for
    the lanes. Each placement runs on its own runtime: one per device that holds a
    model alone, one per tuple; a device no model uses starts nothing.

    A model on several placements loads once per placement. Under thread isolation
    the first placement loads the registered object and every other placement a
    deep copy made at the first start (host memory for one more copy of the
    weights per placement, and the model must be deep-copyable); under process
    isolation each placement's process has its own unpickled copy.

    At ``start()`` the allocator cap (``memory_cap_gb``, per device) is set
    before any weights load; under process isolation a device used by several
    placements gives each placement's process an equal share of its cap. Every
    model is loaded in ``dtype``, warmed up on its ``example()`` batch and
    measured, and the footprint is reported in the units' ``load().extra`` and
    in ``stats()``. ``spec()`` is identity only, the same before and after start.

    ``schedule`` is ``"co_resident"``, ``"swappable"`` or ``"priority_streams"``
    (see ``runtime``). ``isolation="thread"`` runs each device on threads of the
    machine's process; ``isolation="process"`` runs each device in its own
    process, with tensors crossing through shared memory (``transport="shm"``)
    or pipes (``"pickle"``). ``slots`` is each unit's number of batch slots
    (default 2, 8 under ``swappable`` so a waiting model's queue fills).

    Cancel: a batch not yet running is skipped. Under process isolation a
    cancelled batch still running (or queued behind one) after
    ``cancel_kill_s`` seconds has its device process killed and restarted, so
    the unit's slots come back; the device's other batches in flight fail with
    ``WorkerDied`` (None never kills). Under thread isolation a running batch
    cannot be stopped: its slot stays busy until ``forward`` returns.

    A device process that dies is restarted at once; when the restart fails the
    device is lost, and ``ready()`` tries again after a backoff that starts at
    ``revive_backoff_s[0]`` and doubles up to ``revive_backoff_s[1]``.
    """

    def __init__(
        self,
        devices: list[str] | tuple[str, ...] = ("cuda:0",),
        memory_cap_gb: float | None = None,
        schedule: str = "priority_streams",
        isolation: str = "thread",
        dtype: str = "bfloat16",
        slots: int | None = None,
        prep_workers: int = 2,
        swap_quantum_ms: float = 50.0,
        transport: str = "shm",
        shm_threshold: int = SHM_THRESHOLD,
        start_timeout_s: float = 300.0,
        cancel_kill_s: float | None = 5.0,
        revive_backoff_s: tuple[float, float] = (1.0, 60.0),
        name: str = "torch",
    ):
        match schedule, isolation, transport:
            case (s, ("thread" | "process"), ("shm" | "pickle")) if s in SCHEDULES:
                pass
            case _:
                raise ValueError(
                    f"schedule / isolation / transport: {schedule, isolation, transport}"
                )
        if not isinstance(getattr(torch, dtype, None), torch.dtype):
            raise ValueError(f"dtype {dtype!r} is not a torch dtype")
        self.name = name
        self.devices = list(devices)
        self.memory_cap_gb = memory_cap_gb
        self.schedule = schedule
        self.isolation = isolation
        self.dtype = dtype
        self.slots = slots or (8 if schedule == "swappable" else 2)
        self.prep_workers = prep_workers
        self.swap_quantum_ms = swap_quantum_ms
        self.transport = transport
        self.shm_threshold = shm_threshold
        self.start_timeout_s = start_timeout_s
        self.cancel_kill_s = cancel_kill_s
        self.revive_backoff_s = tuple(revive_backoff_s)

        # Guarded by ``lock``: state, the start in progress, backends, unit start counts
        # and in-flight batches.
        self.lock = threading.Lock()
        self.state = EngineState.STOPPED
        self.starting: _Start | None = None
        self.models: dict[str, _Entry] = {}
        self.units_by_key: dict[tuple[str, str], TorchUnit] = {}
        self.backends: dict[str, Any] = {}
        self.footprint: dict[str, dict] = {}
        self.started_units: set[str] = set()

    # ------------------------------------------------------------ models and units
    def add_model(
        self,
        name: str,
        model: Any,
        priority: int = 0,
        devices: list[str | tuple[str, ...]] | None = None,
    ) -> None:
        """Register ``model`` on ``devices`` (default: the engine's first device); one unit per
        entry, a tuple entry being one model split across those devices."""
        with self.lock:
            if self.state != EngineState.STOPPED:
                raise RuntimeError(f"{self.name}: models are added before start()")
        if name in self.models:
            raise ValueError(f"{self.name}: model {name!r} already added")

        # Placements: each entry as a device tuple, every device one of the engine's
        on = [placement(d) for d in (devices or self.devices[:1])]
        unknown = sorted({d for p in on for d in p if d not in self.devices})
        if unknown:
            raise ValueError(f"{self.name}: devices {unknown} are not in {self.devices}")
        if len(set(on)) != len(on):
            raise ValueError(f"{self.name}: model {name!r} placed twice on one placement")

        self.models[name] = _Entry(model, priority, on)
        for p in on:
            self.units_by_key[(name, label(p))] = TorchUnit(self, name, p)

    def unit(self, name: str, device: str | tuple[str, ...] | None = None) -> "TorchUnit":
        """The batch unit of model ``name`` (on ``device``, a device or a split's tuple, when
        the model has several placements)."""
        entry = self.models[name]
        if device is None:
            if len(entry.placements) != 1:
                raise ValueError(f"model {name!r} is on {entry.devices}; name a device")
            return self.units_by_key[(name, label(entry.placements[0]))]
        return self.units_by_key[(name, label(placement(device)))]

    def units(self, name: str | None = None) -> list["TorchUnit"]:
        """Every unit, or every unit of model ``name``."""
        return [u for (m, _), u in self.units_by_key.items() if name is None or m == name]

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start every device: cap, load, warm up, measure. Idempotent.

        One start runs at a time: a caller arriving while it runs waits for it and
        gets its error.
        """
        with self.lock:
            match self.state:
                case EngineState.UP:
                    return
                case EngineState.STARTING:
                    starting, own = self.starting, False
                case _:
                    starting, own = _Start(), True
                    self.starting = starting
                    self.state = EngineState.STARTING
        if not own:
            starting.wait()
            return

        try:
            backends = self._open()
        except BaseException as exc:
            with self.lock:
                self.state = EngineState.STOPPED
            starting.finish(exc)
            raise
        with self.lock:
            self.backends = backends
            self.state = EngineState.UP
        starting.finish(None)

    def placements(self) -> list[tuple[str, ...]]:
        """What the engine starts: each engine device that holds a one-device model, then each
        split tuple, in order of first use; a device no model uses starts nothing."""
        split = [p for e in self.models.values() for p in e.placements if len(p) > 1]
        alone = {p[0] for e in self.models.values() for p in e.placements if len(p) == 1}
        singles = [(d,) for d in self.devices if d in alone]
        return list(dict.fromkeys([*singles, *split]))

    def _caps(self, places: list[tuple[str, ...]]) -> list[Any]:
        """Each placement's allocator cap: the engine's, or under process isolation a share
        per device of it (the cap over the processes using that device)."""
        if self.memory_cap_gb is None or self.isolation == "thread":
            return [self.memory_cap_gb] * len(places)
        users = {d: sum(d in p for p in places) for p in places for d in p}
        return [{d: self.memory_cap_gb / users[d] for d in p} for p in places]

    def _open(self) -> dict[str, Any]:
        """Start every placement's backend; on a failure the started ones stop again."""
        places = self.placements()
        backends: dict[str, Any] = {}

        # Every placement's model object exists before any of them loads
        shared = self.isolation == "thread"
        for e in self.models.values():
            for p in e.placements:
                e.instance(p, shared)

        try:
            for p, cap in zip(places, self._caps(places)):
                runtime = {
                    "schedule": self.schedule,
                    "dtype": self.dtype,
                    "memory_cap_gb": cap,
                    "prep_workers": self.prep_workers,
                    "swap_quantum_ms": self.swap_quantum_ms,
                }
                # Each model this placement holds, as this placement's own object
                models = {
                    n: (e.instance(p, shared), e.priority)
                    for n, e in self.models.items()
                    if p in e.placements
                }
                device = label(p)
                match self.isolation:
                    case "thread":
                        backend = LocalDevice(p, models, runtime)
                    case _:
                        backend = RemoteDevice(
                            p,
                            models,
                            runtime,
                            shm=self.transport == "shm",
                            threshold=self.shm_threshold,
                            start_timeout_s=self.start_timeout_s,
                            cancel_kill_s=self.cancel_kill_s,
                            revive_backoff_s=self.revive_backoff_s,
                        )
                backend.on_revived = partial(self._revived, device)
                self.footprint[device] = backend.start()
                backends[device] = backend
        except BaseException:
            for backend in backends.values():
                backend.stop()
            raise
        return backends

    def stop(self) -> None:
        """Stop every device; batches not yet answered get ``UnitError``.

        A stop arriving while a start runs waits for the start, then stops.
        """
        while True:
            with self.lock:
                match self.state:
                    case EngineState.STARTING:
                        starting = self.starting
                    case EngineState.UP:
                        self.state = EngineState.STOPPED
                        backends, self.backends = self.backends, {}
                        self.started_units.clear()
                        break
                    case _:
                        return
            starting.settled.wait()
        for backend in backends.values():
            backend.stop()

    def ready(self) -> bool:
        """Every device runs; a lost device process starts its revival (``revive_backoff_s``)."""
        with self.lock:
            if self.state != EngineState.UP:
                return False
            backends = list(self.backends.values())
        for backend in backends:
            if not backend.alive:
                backend.revive()
        return all(b.alive for b in backends)

    def spec(self) -> dict:
        return {
            "engine": "TorchEngine",
            "name": self.name,
            "devices": self.devices,
            "dtype": self.dtype,
            "schedule": self.schedule,
            "isolation": self.isolation,
            "memory_cap_gb": self.memory_cap_gb,
            "models": {n: e.devices for n, e in self.models.items()},
        }

    def stats(self) -> dict:
        """Per placement (by name): scheduler counters (batches, items, busy time, swaps,
        restarts), footprint.
        """
        with self.lock:
            backends = dict(self.backends)
        return {
            device: {**backend.stats(), "footprint": self.footprint.get(device, {})}
            for device, backend in backends.items()
        }

    # ------------------------------------------------------------ unit plumbing
    def _unit_start(self, unit: "TorchUnit") -> None:
        self.start()
        with self.lock:
            self.started_units.add(unit.name)

    def _unit_stop(self, unit: "TorchUnit") -> None:
        with self.lock:
            self.started_units.discard(unit.name)
            last = not self.started_units
        if last:
            self.stop()

    def _backend(self, device: str, revive: bool = False) -> Any:
        """The device's running backend, or None; ``revive`` starts a lost one's revival."""
        with self.lock:
            backend = self.backends.get(device) if self.state == EngineState.UP else None
        if backend is None:
            return None
        if revive and not backend.alive:
            backend.revive()
        return backend if backend.alive else None

    def _submit(self, unit: "TorchUnit", items: list, done: Any) -> Ticket:
        ticket = Ticket(items, done)
        backend = self._backend(unit.device)
        if backend is None:
            ticket.answer_all(UnitError(EngineStopped(f"{unit.name} is not running")))
            return ticket
        with self.lock:
            unit.inflight += 1
        job = Job(
            id=ticket.id,
            model=unit.model_name,
            n=len(items),
            payloads=partial(_payloads, items),
            reply=partial(self._reply, unit, ticket),
        )
        ticket.handle = (backend, job)
        backend.put(job)
        return ticket

    def _reply(self, unit: "TorchUnit", ticket: Ticket, job: Job, outcome: Any) -> None:
        with self.lock:
            unit.inflight -= 1
        if not ticket.answered:
            ticket.answer(_results(ticket.items, outcome))
        else:
            # A cancelled batch's slot frees without a completion
            unit.wake()

    def _revived(self, device: str) -> None:
        """A restarted placement process (``device``: its name) is back: its units have
        capacity again."""
        for (_, dev), unit in list(self.units_by_key.items()):
            if dev == device:
                unit.wake()

    def _cancel(self, ticket: Any) -> None:
        if not isinstance(ticket, Ticket) or not ticket.answer_all(Cancelled()):
            return
        if ticket.handle is not None:
            backend, job = ticket.handle
            backend.cancel(job)


class TorchUnit:
    """One model on one placement (a device, or the devices a split model spans), as a batch
    engine; ``capacity()`` is its free batch slots."""

    kind = "batch"
    protocol = "torch"

    def __init__(self, engine: TorchEngine, model_name: str, devices: tuple[str, ...]):
        entry = engine.models[model_name]
        self.engine = engine
        self.model_name = model_name
        self.devices = devices
        self.device = label(devices)
        self.name = f"{engine.name}:{model_name}@{self.device}"
        self.shares_memory = engine.isolation == "thread"
        self.max_batch = int(entry.model.max_batch)
        self.inflight = 0
        self._wakes: list = []

    @property
    def model(self) -> Any:
        """The model object this placement loads in this process."""
        entry = self.engine.models[self.model_name]
        return entry.instance(self.devices, self.engine.isolation == "thread")

    def add_wake(self, fn: Any) -> None:
        self._wakes.append(fn)

    def wake(self) -> None:
        """Call every added wake function: capacity rose without a completion."""
        for fn in list(self._wakes):
            fn()

    def start(self) -> None:
        self.engine._unit_start(self)

    def stop(self) -> None:
        self.engine._unit_stop(self)

    def ready(self) -> bool:
        return self.engine._backend(self.device, revive=True) is not None

    def spec(self) -> dict:
        return {
            "engine": "TorchEngine",
            "name": self.name,
            "model": self.model_name,
            "device": self.device,
            "devices": list(self.devices),
            "dtype": self.engine.dtype,
            "schedule": self.engine.schedule,
            "isolation": self.engine.isolation,
            "max_batch": self.max_batch,
        }

    def capacity(self) -> int:
        if self.engine._backend(self.device) is None:
            return 0
        with self.engine.lock:
            return max(0, self.engine.slots - self.inflight)

    def load(self) -> Load:
        with self.engine.lock:
            backend = self.engine.backends.get(self.device)
            running = self.inflight
        stats = backend.stats() if backend is not None else {}
        extra = {
            "footprint": self.engine.footprint.get(self.device, {}).get(self.model_name, {}),
            "model": stats.get("models", {}).get(self.model_name, {}),
            "swaps": stats.get("swaps", 0),
            "swap_s": stats.get("swap_s", 0.0),
            "restarts": stats.get("restarts", 0),
            "kills": stats.get("kills", 0),
        }
        return Load(running=running, waiting=0, extra=extra)

    def key(self, item: Any) -> Hashable:
        """The model's key of the payload for a light adapter; else the adapter's own key.

        A heavy adapter prepares on the engine's threads only: its items share a
        batch by ``adapter.key(inputs, protocol)`` when it has one, and run alone
        otherwise.
        """
        adapter = item.adapter
        if getattr(adapter, "light", False):
            return self.model.key(item.payload())
        keyer = getattr(adapter, "key", None)
        if keyer is not None:
            return keyer(item.inputs, item.protocol)
        return ("alone", id(item))

    def submit(self, work: Any, done: Any) -> Ticket:
        items = work if isinstance(work, list) else [work]
        return self.engine._submit(self, items, done)

    def cancel(self, ticket: Any) -> None:
        self.engine._cancel(ticket)
