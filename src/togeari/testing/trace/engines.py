"""Scripted engines: no threads, answers in virtual time, in an order a seed gives.

A ``ScriptedEngine`` keeps its submissions on the driver's ``Schedule``. Each submission
is due at ``now + delay`` on the ``VirtualClock``; the delay, and whether each item fails,
come from a generator seeded by the trace seed and the engine's name, drawn in submit order.
The driver fires due submissions between dispatcher cycles; the node's Python function runs
then (``PythonAdapter`` payload), on the driver's thread.
"""

import heapq
import itertools
import random
from collections.abc import Callable
from typing import Any

from togeari.core.contracts.engine import EngineBase, Load
from togeari.core.contracts.results import Cancelled, ItemError, Ok, UnitError
from togeari.engines.common.errors import EngineStopped
from togeari.testing.sim import SimError, SimOutage


class VirtualClock:
    """Seconds that pass only when the driver moves them."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance_to(self, t: float) -> None:
        if t > self.now:
            self.now = t


class Schedule:
    """Due submissions of every scripted engine, by (due time, submit order)."""

    def __init__(self, clock: VirtualClock):
        self.clock = clock
        self.heap: list = []
        self.pending: dict[int, Callable[[], None]] = {}
        self.seq = itertools.count(1)

    def at(self, due: float, fire: Callable[[], None]) -> int:
        n = next(self.seq)
        heapq.heappush(self.heap, (due, n))
        self.pending[n] = fire
        return n

    def drop(self, n: int) -> bool:
        return self.pending.pop(n, None) is not None

    def next_due(self) -> float | None:
        h = self.heap
        while h and h[0][1] not in self.pending:
            heapq.heappop(h)
        return h[0][0] if h else None

    def fire_due(self) -> int:
        """Fire every submission due by now, in (due, submit order); returns how many."""
        n = 0
        now = self.clock()
        while (due := self.next_due()) is not None and due <= now:
            _, k = heapq.heappop(self.heap)
            self.pending.pop(k)()
            n += 1
        return n


class ScriptedEngine(EngineBase):
    """A call, batch or stream unit answering in virtual time.

    ``latency_ms`` sets the mean delay; ``tail`` the weight of rare long delays;
    ``item_error_rate`` the share of items answered with ``ItemError``; ``outages`` are
    ``(start, end)`` virtual seconds when the unit is down (``ready()`` false, submits
    answered with ``UnitError``).
    """

    def __init__(
        self,
        kind: str = "call",
        name: str | None = None,
        latency_ms: float = 1.0,
        tail: float = 1.0,
        capacity: int = 8,
        max_batch: int = 16,
        item_error_rate: float = 0.0,
        outages: list[tuple[float, float]] | None = None,
    ):
        super().__init__(name or f"scripted-{kind}")
        self.kind = kind
        self.latency = latency_ms / 1000.0
        self.tail = tail
        self.cap = capacity
        self.max_batch = max_batch
        self.item_error_rate = item_error_rate
        self.outages = list(outages or [])
        self.schedule: Schedule | None = None
        self.rng: random.Random | None = None
        self.inflight = 0
        self.tickets: dict[int, tuple[list, Any]] = {}
        self.counts = {"submits": 0, "items": 0, "errors": 0, "outage_rejects": 0}

    def bind(self, schedule: Schedule, seed: int) -> None:
        """Join a trace: its schedule, and the generator of this seed and name."""
        self.schedule = schedule
        self.rng = random.Random(f"{seed}/{self.name}")

    # ------------------------------------------------------------ state
    def in_outage(self) -> bool:
        if self.schedule is None:
            return False
        t = self.schedule.clock()
        return any(a <= t < b for a, b in self.outages)

    def ready(self) -> bool:
        return not self.in_outage()

    def spec(self) -> dict:
        return {"engine": "scripted", "name": self.name, "kind": self.kind}

    def capacity(self) -> int:
        if self.in_outage():
            return 1
        return max(self.cap - self.inflight, 0)

    def load(self) -> Load:
        return Load(running=self.inflight, waiting=0)

    def stop(self) -> None:
        """Every pending ticket is answered with ``UnitError``."""
        pending, self.tickets = self.tickets, {}
        self.inflight = 0
        for t, (items, done) in pending.items():
            if self.schedule is not None:
                self.schedule.drop(t)
            done(t, [UnitError(EngineStopped(f"{self.name} stopped"))] * len(items))

    # ------------------------------------------------------------ work
    def delay(self) -> float:
        rng = self.rng
        base = self.latency * (0.5 + rng.random())
        return base * (1.0 + self.tail * rng.random() ** 4 * 20.0)

    def submit(self, work: Any, done: Any) -> int:
        if self.schedule is None:
            raise RuntimeError(f"{self.name}: a scripted engine runs under the trace driver only")
        items = work if isinstance(work, list) else [work]
        self.counts["submits"] += 1
        self.counts["items"] += len(items)

        # Outage: the whole unit is blocked
        if self.in_outage():
            self.counts["outage_rejects"] += 1
            done(None, [UnitError(SimOutage(f"{self.name} is down"))] * len(items))
            return -1

        # Delay and failures drawn now, in submit order
        fails = [
            bool(self.item_error_rate) and self.rng.random() < self.item_error_rate for _ in items
        ]
        due = self.schedule.clock() + self.delay()
        cost = 1 if self.kind == "batch" else len(items)
        self.inflight += cost
        box: dict = {}

        def fire() -> None:
            if self.tickets.pop(box["t"], None) is None:
                return
            self.inflight -= cost
            done(box["t"], [self.run_one(it, bad) for it, bad in zip(items, fails, strict=True)])

        t = box["t"] = self.schedule.at(due, fire)
        self.tickets[t] = (items, done)
        return t

    def run_one(self, item: Any, fail: bool) -> Any:
        if fail:
            self.counts["errors"] += 1
            return ItemError(SimError(f"injected failure in {item.node}"))
        try:
            payload = item.payload()
            raw = payload.run() if hasattr(payload, "run") else payload
            return Ok(item.finish(raw), True)
        except Exception as exc:  # noqa: BLE001
            return ItemError(exc)

    def cancel(self, ticket: Any) -> None:
        if self.schedule is None or ticket is None or ticket < 0:
            return
        if not self.schedule.drop(ticket):
            return
        entry = self.tickets.pop(ticket, None)
        if entry is None:
            return
        items, done = entry
        self.inflight -= 1 if self.kind == "batch" else len(items)
        done(ticket, [Cancelled()] * len(items))


def ScriptedCall(**kw: Any) -> ScriptedEngine:
    return ScriptedEngine("call", **kw)


def ScriptedBatch(**kw: Any) -> ScriptedEngine:
    kw.setdefault("capacity", 2)
    return ScriptedEngine("batch", **kw)


def ScriptedStream(**kw: Any) -> ScriptedEngine:
    kw.setdefault("capacity", 256)
    return ScriptedEngine("stream", **kw)
