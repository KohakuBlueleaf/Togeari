"""Lanes of the native core: ``adopt`` gives a ``Lane`` its queues and issue logic in
``togeari._rs.Lane`` (spec/11 §11.3). A subclass of ``Lane`` is adopted only when it overrides
none of what the native lane runs; it then keeps its class as a base of its native class.

Everything the machine, plugins and queries read of an adopted lane reads the native state.
Settings set on the lane (``max_inflight``, ``retry``, ``timeout_s``, ``lane_dead_s``, ``name``,
``units``, ``former``, ``select``, ``forming``, ``clock``) reach the native lane; ``retry``'s
fields are read when a retry happens. A method of ``DIRECT`` set on the instance is called
wherever the Python lane would call it (the core, the native lane, a builtin selector). A
``Lane`` internal the native lane runs itself raises ``NotImplementedError``.
"""

from dataclasses import dataclass, field
from typing import Any

from togeari.core.contracts.policies import Former, Selector
from togeari.core.lanes.lane import FORMING, Lane, _checked
from togeari.core.lanes.resources import refresh

try:
    from togeari import _rs
except ImportError:
    _rs = None

# Settings a native lane takes from the lane object when they are set
FORWARDED = ("max_inflight", "retry", "timeout_s", "lane_dead_s", "name", "units")

# Methods the core, the native lane or its selector call: one set on the instance is called
# instead, at every point the Python lane calls it
DIRECT = (
    "enqueue",
    "pump",
    "timers",
    "next_deadline",
    "results",
    "cancel",
    "queued",
    "units_up",
    "queued_now",
    "under_cap",
    "load_waiting",
    "remove",
    "unit_down",
    "requeue_front",
    "rate",
)

# Lane internals the native lane runs itself
INTERNAL = (
    "_set_state",
    "_poll",
    "_move_to_shared",
    "_check_drained",
    "_detached",
    "_fail_queued",
    "_hold",
    "_route",
    "_spread",
    "_respread",
    "_noting",
    "_order",
    "_admits",
    "_count",
    "_submit",
    "_arm_timeout",
    "_close",
    "_join",
    "_leave",
    "_units_changed",
    "_queues",
    "_cap",
    "_digest_specs",
    "_timing",
    "_waiting",
    "_wake_times",
    "_dead",
    "_policy_failed",
)

# Python lane state an adopted lane no longer keeps
STATE = (
    "_q",
    "state",
    "_held",
    "_held_ids",
    "_timeouts",
    "subs",
    "inflight",
    "no_unit_since",
    "no_unit_counted",
    "stats",
    "batch_sizes",
    "waits",
    "_up",
    "up_version",
    "_pump_needed",
    "_pumped_at",
    "_specs",
    "_identity",
    "_seq",
)


@dataclass
class UnitView:
    """A unit's state in a native lane, as ``Lane.state`` holds it."""

    unit: Any
    state: str
    inflight: int
    subs: dict
    rate: float
    busy_since: float | None
    busy_total: float
    items_done: int
    errors: dict = field(default_factory=dict)
    next_poll: float = 0.0
    backoff: float = 0.5
    submit_lat: float = 0.0


def _internal(name: str) -> Any:
    def refuse(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(f"{name}: the native lane of {self.name!r} runs this itself")

    refuse.__name__ = name
    return refuse


def _forwarded(name: str) -> property:
    key = f"_set_{name}"

    def get(self: Any) -> Any:
        return self.__dict__[key]

    def set(self: Any, value: Any) -> None:
        if name == "units" and not isinstance(value, list):
            value = list(value)
        self.__dict__[key] = value
        getattr(self._native, f"set_{name}")(value)

    return property(get, set, doc=f"The lane's ``{name}`` (the native lane follows it).")


class NativeLane(Lane):
    """A lane whose queues and issue logic run natively; built by ``adopt``."""

    _native: Any

    def __setattr__(self, name: str, value: Any) -> None:
        if name in DIRECT:
            self._native.set_overridden(name, True)
        Lane.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        object.__delattr__(self, name)
        if name in DIRECT:
            self._native.set_overridden(name, False)

    # ------------------------------------------------------------ the dispatcher interface
    def enqueue(self, item: Any, now: float) -> None:
        self._native.enqueue(item, now)

    def pump(self, now: float) -> int:
        return self._native.pump(now)

    def timers(self, now: float) -> list:
        return self._native.timers(now)

    def next_deadline(self, now: float) -> float | None:
        return self._native.next_deadline(now)

    def results(self, sub: Any, results: list, now: float) -> list:
        return self._native.results(sub, results, now)

    def remove(self, item: Any) -> bool:
        return self._native.remove(item)

    def cancel(self, item: Any) -> None:
        self._native.cancel(item)

    def attach(self, unit: Any) -> None:
        self._native.attach(unit)

    def detach(self, unit: Any, drain: bool, now: float) -> list:
        return self._native.detach(unit, drain, now)

    def unit_down(self, unit: Any, err: Any, now: float) -> None:
        self._native.unit_down(unit, err, now)

    def requeue_front(self, item: Any, now: float | None = None) -> None:
        self._native.requeue_front(item, now)

    def capacity_rose(self) -> None:
        self._native.capacity_rose()

    # ------------------------------------------------------------ views
    def units_up(self) -> list:
        return self._native.units_up()

    @property
    def up_version(self) -> int:
        return self._native.up_version

    def rate(self, unit: Any) -> float:
        return self._native.rate(unit)

    def queued_now(self) -> int:
        return self._native.queued_now()

    def queued(self) -> int:
        return self._native.queued()

    def load_waiting(self) -> int:
        return self._native.load_waiting()

    def _resource_held(self):
        return self._native.resource_held()

    def _wait_item(self, item, now, limit=1024):
        return self._native.wait_item(item, now, limit)

    def _resource_requeue(self, item, now):
        self._native.resource_requeue(item, now)

    def signals(self) -> dict:
        signals = self._native.signals()
        if self._resources is not None:
            signals["resources"] = self._resources.snapshot()
        return signals

    def take_waits(self) -> list[float]:
        return self._native.take_waits()

    def under_cap(self) -> bool:
        return self._native.under_cap()

    def engine_identity(self) -> bytes:
        return self._native.engine_identity()

    @property
    def queue(self) -> list:
        return self._native.queue()

    @property
    def state(self) -> dict[int, UnitView]:
        return {id(row[0]): UnitView(*row) for row in self._native.unit_rows()}

    @property
    def stats(self) -> dict:
        return self._native.stats()

    @property
    def batch_sizes(self) -> dict:
        return self._native.batch_sizes()

    @property
    def inflight(self) -> int:
        return self._native.inflight

    @property
    def subs(self) -> dict:
        return self._native.subs()

    # ------------------------------------------------------------ settings, policies, clock
    max_inflight = _forwarded("max_inflight")
    retry = _forwarded("retry")
    timeout_s = _forwarded("timeout_s")
    lane_dead_s = _forwarded("lane_dead_s")
    name = _forwarded("name")
    units = _forwarded("units")

    @property
    def former(self) -> Any:
        return self._former

    @former.setter
    def former(self, former: Any) -> None:
        self._former = _checked(former, Former, "former")
        refresh(self, self._former)
        self._native.set_former(self._former, self.clock())

    @property
    def select(self) -> Any:
        return self._select

    @select.setter
    def select(self, select: Any) -> None:
        self._select = _checked(select, Selector, "select")
        self._native.set_select(self._select)

    @property
    def forming(self) -> str:
        return self._forming

    @forming.setter
    def forming(self, forming: str) -> None:
        if forming not in FORMING:
            raise ValueError(f"forming must be one of {FORMING}, not {forming!r}")
        self._forming = forming
        self._native.set_forming(forming)

    @property
    def clock(self) -> Any:
        return self._native.clock

    @clock.setter
    def clock(self, clock: Any) -> None:
        self._native.clock = clock

    def set_telemetry(self, ring: Any) -> None:
        """The telemetry ring each submission is written to: a ``togeari._rs.TelemetryRing``
        (None: none)."""
        if ring is not None and not isinstance(ring, _rs.TelemetryRing):
            raise TypeError("a native lane writes a togeari._rs.TelemetryRing")
        self.telemetry = ring
        self._native.set_telemetry(ring)


for _name in INTERNAL:
    setattr(NativeLane, _name, _internal(_name))

# What a subclass must leave as ``Lane`` has it to be adopted
OWN = tuple(n for n in vars(NativeLane) if not n.startswith("__"))
_classes: dict[type, type] = {}


def native_class(cls: type) -> type | None:
    """The native class of lane class ``cls``; None when ``cls`` overrides something the native
    lane runs (its lanes stay Python lanes)."""
    if cls is Lane:
        return NativeLane
    got = _classes.get(cls)
    if got is None:
        if any(getattr(cls, n, None) is not getattr(Lane, n, None) for n in OWN):
            return None
        got = _classes[cls] = type(f"Native{cls.__name__}", (NativeLane, cls), {})
    return got


def adopt(lane: Lane) -> Lane:
    """``lane``, its queues and issue logic moved to the native core (once); a lane whose class
    overrides what the native lane runs is returned as it is."""
    if isinstance(lane, NativeLane):
        return lane
    cls = native_class(type(lane))
    if cls is None:
        return lane
    native = _rs.Lane(lane)
    native._bind()
    d = lane.__dict__
    for name in FORWARDED:
        d[f"_set_{name}"] = d.pop(name)
    for name in STATE:
        d.pop(name, None)
    for name in DIRECT:
        if name in d:
            native.set_overridden(name, True)
    d["_native"] = native
    lane.__class__ = cls
    return lane
