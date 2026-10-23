"""Lanes: the machine's queue and issue logic for one kind of engine, over one or more units.

A lane holds priority queues, per-unit in-flight accounting and state, and multi-issues
to every unit with free capacity (spec/04 §4.3-§4.5). Every method runs on the dispatcher
thread; engines answer through ``done``, which the dispatcher turns into inbox events.

Queueing is in ``queue``, issuing in ``pump``, unit states and time in ``health``.
"""

import contextlib
import hashlib
import heapq
import itertools
import time
import weakref
from collections.abc import Callable
from typing import Any

from togeari.core.contracts.policies import Former, Selector
from togeari.core.contracts.results import ItemError, Ok, UnitError, UnitUnavailable
from togeari.core.lanes.deadline import current, expire
from togeari.core.lanes.health import Health, Retry, State, UnitState
from togeari.core.lanes.pump import Pump, Submission
from togeari.core.lanes.queue import Queue
from togeari.core.policies.select import LeastLoaded

_lane_ids = itertools.count(1)
FORMING = ("late", "early")


class Lane(Pump, Health):
    """Queue and issue logic for interchangeable units of one engine kind.

    - ``former``: batch forming (batch lanes); ``select``: unit choice.
    - ``forming``: ``"late"`` (one shared queue) or ``"early"`` (per-unit queues filled at
      enqueue; items queued while no unit is up reach unit queues at the next pump).
    - ``max_inflight``: cap on submitted, unsettled items across the lane.
    - ``retry``: re-queues per item after ``UnitError``, each after ``retry.backoff_ms``.
    - ``timeout_s``: per item, from submit; a ``WorkItem.deadline`` set before ``enqueue``
      bounds it, and bounds the time queued too.
    - ``lane_dead_s``: once no unit has been up this long, queued and held items fail with
      ``ItemError(UnitUnavailable)`` and ``lane.dead`` is emitted; None keeps them forever.

    ``former`` and ``select`` are checked against their contracts when set (TypeError on a
    missing method); a string is a name the machine resolves before use, and a resolved
    policy the lane refuses is a construction problem. ``forming`` outside ``FORMING`` is a
    ValueError.

    Dispatcher interface: ``enqueue``, ``pump``, ``timers``, ``next_deadline``, ``results``,
    ``remove`` / ``cancel``, ``attach``, ``detach``, ``engine_identity``.
    """

    def _execution_changed(self, kind: str) -> None:
        notify = self.__dict__.get("_execution_notify")
        if notify is not None:
            notify(self, kind)

    def _watch_retry(self, previous=None) -> None:
        if isinstance(previous, Retry):
            previous.__dict__.get("_execution_watchers", {}).pop(id(self), None)
        retry = getattr(self, "retry", None)
        if isinstance(retry, Retry) and self.__dict__.get("_execution_notify") is not None:
            watchers = retry.__dict__.setdefault("_execution_watchers", {})
            watchers[id(self)] = weakref.WeakMethod(self._execution_changed)

    def __setattr__(self, name, value):
        watched = name in ("timeout_s", "retry", "lane_dead_s")
        previous = None
        if watched and self.__dict__.get("_execution_notify") is not None:
            self._execution_changed("policy_changed")
            previous = getattr(self, name, None)
        object.__setattr__(self, name, value)
        if name == "retry":
            self._watch_retry(previous)
        if watched:
            self._execution_changed("policy_changed")

    def __init__(
        self,
        units: list,
        former: Any = None,
        select: Any = None,
        forming: str = "late",
        max_inflight: int | None = None,
        retry: Retry | None = None,
        timeout_s: float | None = None,
        lane_dead_s: float | None = 300.0,
        name: str | None = None,
    ):
        if not isinstance(units, (list, tuple)):
            units = [units]
        self.name = name or f"lane{next(_lane_ids)}"
        self.units = list(units)
        kinds = {getattr(u, "kind", "call") for u in self.units}
        self.kind = kinds.pop() if len(kinds) == 1 else "mixed"
        self.select = select or LeastLoaded()
        self.forming = forming
        self.max_inflight = max_inflight
        self.retry = retry or Retry()
        self.timeout_s = timeout_s
        self.lane_dead_s = lane_dead_s
        # The clock of the times the lane reads itself (monotonic seconds, the dispatcher's)
        self.clock = time.monotonic

        # Queues: the shared one and one per unit (early forming)
        self._former = _checked(former, Former, "former")
        self._seq = itertools.count()
        key_unit = self.units[0] if self.units else None
        self._q = Queue(former, key_unit, self._cap(), self._policy_failed)
        self.state: dict[int, UnitState] = {}
        for u in self.units:
            self.state[id(u)] = UnitState(u, Queue(former, u, self._q.cap, self._policy_failed))

        # Retries waiting for their backoff, item timeouts
        self._held: list = []  # (ready at, seq, item)
        self._held_ids: dict[int, Any] = {}
        self._timeouts: list = []  # (deadline, seq, item, submission)

        # Submissions, counters
        self.subs: dict[int, Submission] = {}
        self.inflight = 0
        self.no_unit_since: float | None = None
        self.no_unit_counted = 0.0
        self.stats = {
            "submitted": 0,
            "retries": 0,
            "failovers": 0,
            "recoveries": 0,
            "batches": 0,
            "no_unit_s": 0.0,
            "dead_failed": 0,
        }
        self.batch_sizes: dict[int, int] = {}
        self.waits: list[float] = []

        # Units-up cache, pump bookkeeping, engine identity
        self._up: list | None = None
        self.up_version = 0
        self._pump_needed = False
        self._pumped_at = float("-inf")
        self._specs: dict[int, str] = {id(u): _spec(u) for u in self.units}
        self._identity = self._digest_specs()

        # Set by the machine: done(lane, submission, results) and wake(lane) post inbox events;
        # emit(kind, **fields) for lane events, wants(kind) whether a detail event is received
        self.on_done: Callable | None = None
        self.on_wake: Callable | None = None
        self.hook_submit: Callable | None = None
        self.emit: Callable = lambda *a, **k: None
        self.wants: Callable = lambda kind, job=None: True

        # Units that report capacity rising without a completion wake the lane
        self._woken_by: set[int] = set()
        for unit in self.units:
            self._listen(unit)

    # ------------------------------------------------------------ views
    @property
    def former(self) -> Any:
        return self._former

    @former.setter
    def former(self, former: Any) -> None:
        self._former = _checked(former, Former, "former")
        now = self.clock()
        for q in self._queues():
            q.set_former(self._former, now)

    @property
    def forming(self) -> str:
        return self._forming

    @forming.setter
    def forming(self, forming: str) -> None:
        if forming not in FORMING:
            raise ValueError(f"forming must be one of {FORMING}, not {forming!r}")
        self._forming = forming

    @property
    def select(self) -> Any:
        return self._select

    @select.setter
    def select(self, select: Any) -> None:
        self._select = _checked(select, Selector, "select")

    @property
    def queue(self) -> list:
        """Every queued item in queue order (O(n log n); views and tests)."""
        entries = sorted((e.order, e.item) for q in self._queues() for e in q.index.values())
        return [item for _, item in entries]

    def _queues(self) -> list[Queue]:
        return [self._q, *(st.queue for st in self.state.values())]

    def _cap(self) -> int | float:
        return min((max(1, getattr(u, "max_batch", 1) or 1) for u in self.units), default=1)

    def units_up(self) -> list:
        if self._up is None:
            self._up = [s.unit for s in self.state.values() if s.state == State.UP]
        return self._up

    def rate(self, unit: Any) -> float:
        return self.state[id(unit)].rate

    def queued_now(self) -> int:
        """Items in the lane's queues."""
        return len(self._q) + sum(len(st.queue) for st in self.state.values())

    def queued(self) -> int:
        """Items waiting in the lane: queued, and retries waiting for their backoff."""
        return self.queued_now() + len(self._held_ids)

    def load_waiting(self) -> int:
        total = 0
        for unit in self.units_up():
            with contextlib.suppress(Exception):
                total += int(unit.load().waiting)
        return total

    def signals(self) -> dict:
        return {
            "queue": self.queued(),
            "inflight": self.inflight,
            "units_up": len(self.units_up()),
            "waiting": self.load_waiting(),
        }

    def take_waits(self) -> list[float]:
        """Queue waits of the items submitted since the last call (and forget them)."""
        waits, self.waits = self.waits, []
        return waits

    def under_cap(self) -> bool:
        return self.max_inflight is None or self.inflight < self.max_inflight

    def engine_identity(self) -> bytes:
        """The engine identity for content keys: a digest of the distinct ``spec()`` (without
        ``name``) of the lane's units, each read when the unit joins (construction or
        ``attach``). A unit of an engine config already in the lane leaves it unchanged."""
        return self._identity

    def _digest_specs(self) -> bytes:
        specs = sorted({self._specs[id(u)] for u in self.units})
        return hashlib.sha256("\n".join(specs).encode()).digest()

    # ------------------------------------------------------------ queueing
    def enqueue(self, item: Any, now: float) -> None:
        """Queue ``item``; a ``WorkItem.deadline`` it carries also bounds its time queued."""
        item.ext.setdefault("retries", 0)
        item.ext["enqueued"] = now
        if "deadline_cap" not in item.ext:
            item.ext["deadline_cap"] = item.deadline
            if item.deadline is not None:
                heapq.heappush(self._timeouts, (item.deadline, next(self._seq), item, None))
        self._route(item, (-item.priority, next(self._seq)), now)

    def remove(self, item: Any) -> bool:
        """Drop a queued or held item (squashed); True if it was there."""
        now = self.clock()
        if self._held_ids.pop(id(item), None) is not None:
            return True
        return any(q.remove(item, now) for q in self._queues())

    def cancel(self, item: Any) -> None:
        """A squashed item: drop it from the queue, or cancel its submission when all its items
        are squashed."""
        item.ext["squashed"] = True
        if self.remove(item):
            return
        sub = self.subs.get(item.ext.get("sub"))
        if sub is None:
            return
        if all(it.ext.get("squashed") for it in sub.items):
            with contextlib.suppress(Exception):
                sub.unit.cancel(sub.ticket)

    # ------------------------------------------------------------ results
    def results(self, sub: Submission, results: list, now: float) -> list[tuple[Any, Any]]:
        """Settle a submission's results once; returns ``(item, Ok | ItemError | Cancelled)``
        pairs to deliver. ``UnitError`` re-queues items (after the retry backoff) within
        ``retry.max`` and takes the unit down."""
        if not sub.open:
            return []
        st = self._close(sub, now)
        self._pump_needed = True

        # A result count that differs from the item count fails every item
        if len(results) != len(sub.items):
            err = RuntimeError(
                f"engine {sub.unit.name!r} returned {len(results)} results"
                f" for {len(sub.items)} items"
            )
            results = [ItemError(err)] * len(sub.items)

        # Per item: deliver, or retry after a unit error
        out: list[tuple[Any, Any]] = []
        retry: list = []
        unit_error = None
        for it, r in zip(sub.items, results):
            if it.ext.get("settled"):
                if isinstance(r, UnitError):
                    unit_error = r
                continue
            if isinstance(r, UnitError):
                unit_error = r
            expired = expire(it, now, True)
            if expired is not None:
                r = expired
            match r:
                case UnitError():
                    unit_error = r
                    if it.ext.get("squashed"):
                        continue
                    if it.ext.get("ordered_once") or it.ext["retries"] >= self.retry.max:
                        it.ext["settled"] = True
                        err = UnitUnavailable(f"{sub.unit.name}: {r.error}")
                        out.append((it, ItemError(err)))
                    else:
                        retry.append(it)
                    continue
                case Ok():
                    st.items_done += 1
                    dt = max(1e-6, now - it.ext.get("sent", now))
                    st.rate = 0.8 * st.rate + 0.2 / dt if st.rate else 1.0 / dt
                case ItemError():
                    k = type(r.error).__name__
                    st.errors[k] = st.errors.get(k, 0) + 1
            it.ext["settled"] = True
            out.append((it, r))

        # Unit down first, so retried items go to other units
        if unit_error is not None:
            self.unit_down(sub.unit, unit_error, now)
        for it in retry:
            it.ext["retries"] += 1
            self.stats["retries"] += 1
            self._hold(it, now)
        self._check_drained(st, now)
        return out

    # ------------------------------------------------------------ time
    def next_deadline(self, now: float) -> float | None:
        """The earliest time the lane has something to do; a past time only when an action is
        possible now.

        Counted: down units' health polls, retry backoffs, item timeouts, and, while items
        are queued: with a unit up, a pump when something changed since the last one, future
        former deadlines and the ``wake_at()`` of units up (engines that report it) past the
        last pump; with no unit up, the dead-lane limit.
        """
        # Health polls
        ds: list[float] = [st.next_poll for st in self.state.values() if st.state == State.DOWN]

        # Retries waiting for their backoff
        held = self._held
        while held and id(held[0][2]) not in self._held_ids:
            heapq.heappop(held)
        if held:
            ds.append(held[0][0])

        # Item timeouts
        tq = self._timeouts
        while tq and not self._timing(tq[0][2], tq[0][3]):
            heapq.heappop(tq)
        if tq:
            ds.append(tq[0][0])

        # Queued work
        if self.queued_now():
            up = self.units_up()
            if not up:
                if self.lane_dead_s is not None and self.no_unit_since is not None:
                    ds.append(self.no_unit_since + self.lane_dead_s)
            else:
                if self._pump_needed:
                    ds.append(now)
                for q in self._queues():
                    t = q.next_time()
                    if t is not None:
                        ds.append(t)
                ds.extend(self._wake_times(up))
        return min(ds) if ds else None

    def _timing(self, item: Any, sub: Any) -> bool:
        """An attempt timer applies while its submission is open; the original call cap
        stays active across queueing, attempts and backoff until settlement or squash."""
        item = current(item)
        if item.ext.get("settled") or item.ext.get("squashed"):
            return False
        if sub is not None:
            return sub.open
        # The original call cap remains armed through submissions and retry backoff.
        return True

    def _waiting(self, item: Any) -> bool:
        return id(item) in self._held_ids or any(id(item) in q.index for q in self._queues())

    def _wake_times(self, up: list) -> list[float]:
        """``wake_at()`` of units up that have queued work, when past the last pump."""
        out = []
        shared = len(self._q) > 0
        for unit in up:
            wake_at = getattr(unit, "wake_at", None)
            if wake_at is None or not (shared or len(self.state[id(unit)].queue)):
                continue
            try:
                t = wake_at()
            except Exception:  # noqa: BLE001
                t = None
            if t is not None and t > self._pumped_at:
                out.append(t)
        return out

    # ------------------------------------------------------------ elastic units
    def _join(self, unit: Any, now: float) -> UnitState:
        """``unit`` joins ``units``: its spec is read, and the batch cap, the shared queue's key
        unit and the engine identity follow."""
        self.units.append(unit)
        self._listen(unit)
        self._specs[id(unit)] = _spec(unit)
        st = self.state.get(id(unit))
        if st is None:
            st = self.state[id(unit)] = UnitState(
                unit, Queue(self._former, unit, self._q.cap, self._policy_failed)
            )
        self._units_changed(now)
        return st

    def _leave(self, st: UnitState, now: float) -> None:
        """A detached unit leaves ``units``; its state stays for views and a later attach."""
        if st.unit in self.units:
            self.units.remove(st.unit)
        self._units_changed(now)

    def _listen(self, unit: Any) -> None:
        """Register the lane's wake with a unit that offers ``add_wake`` (once per unit)."""
        add_wake = getattr(unit, "add_wake", None)
        if add_wake is not None and id(unit) not in self._woken_by:
            self._woken_by.add(id(unit))
            add_wake(self._woken)

    def _woken(self) -> None:
        """A unit's capacity rose without a completion (any thread)."""
        if self.on_wake is not None:
            self.on_wake(self)

    def capacity_rose(self) -> None:
        """The next pump is due (dispatcher thread)."""
        self._pump_needed = True

    def _units_changed(self, now: float) -> None:
        cap = self._cap()
        for q in self._queues():
            if q.cap != cap:
                q.set_cap(cap, now)
        if self._q.key_unit not in self.units:
            self._q.key_unit = self.units[0] if self.units else None
        self._identity = self._digest_specs()


def _checked(policy: Any, contract: type, what: str) -> Any:
    """``policy`` when it has every method ``contract`` declares (None and names pass)."""
    if policy is None or isinstance(policy, str):
        return policy
    declared = [n for n, v in vars(contract).items() if callable(v) and not n.startswith("_")]
    missing = [n for n in declared if not callable(getattr(policy, n, None))]
    if missing:
        name = type(policy).__name__
        raise TypeError(f"{what} {name} lacks {', '.join(missing)} ({contract.__name__} contract)")
    return policy


def _spec(unit: Any) -> str:
    """The unit's ``spec()`` without its ``name``: units of one engine config share it."""
    try:
        spec = unit.spec()
    except Exception as exc:  # noqa: BLE001
        return repr(("spec-error", getattr(unit, "name", None), type(exc).__name__))
    if isinstance(spec, dict):
        spec = {k: v for k, v in sorted(spec.items()) if k != "name"}
    return repr(spec)


__all__ = ["Lane", "Retry", "State", "Submission", "UnitState"]
