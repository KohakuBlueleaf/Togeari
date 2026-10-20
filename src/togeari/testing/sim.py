"""Simulated engines for checks and examples: latency, capacity, order, failures (spec/07 §7.2).

A simulated engine runs the node's Python function (``PythonAdapter`` payload)
after a simulated delay on its own timer thread. Delays are heavy-tailed and
differ by input; a seed shuffles completion order; ``item_error_rate`` injects
``ItemError``s; ``outages`` (seconds after start) block the whole unit.
"""

import hashlib
import heapq
import itertools
import random
import threading
import time
from typing import Any

from togeari.core.contracts.engine import EngineBase, Load
from togeari.core.contracts.results import Cancelled, ItemError, Ok, UnitError
from togeari.engines.common.errors import EngineStopped


class SimError(RuntimeError):
    """An injected item failure."""


class SimOutage(ConnectionError):
    """An injected unit outage."""


class _Clock(threading.Thread):
    """One timer thread per engine: delivers results when they are due.

    A dropped timer stays in the heap and is skipped when it comes due.
    """

    def __init__(self, name: str):
        super().__init__(name=f"sim-{name}", daemon=True)
        self.heap: list = []
        self.pending: set[int] = set()
        self.cv = threading.Condition()
        self.alive = True
        self.seq = itertools.count()

    def at(self, due: float, fn) -> int:
        with self.cv:
            n = next(self.seq)
            heapq.heappush(self.heap, (due, n, fn))
            self.pending.add(n)
            self.cv.notify()
            return n

    def drop(self, n: int) -> bool:
        with self.cv:
            if n not in self.pending:
                return False
            self.pending.discard(n)
            return True

    def run(self) -> None:
        while self.alive:
            with self.cv:
                while self.alive and (not self.heap or self.heap[0][0] > time.monotonic()):
                    timeout = None if not self.heap else self.heap[0][0] - time.monotonic()
                    self.cv.wait(timeout)
                if not self.alive:
                    return
                _, n, fn = heapq.heappop(self.heap)
                if n not in self.pending:
                    continue
                self.pending.discard(n)
            fn()

    def stop(self) -> None:
        with self.cv:
            self.alive = False
            self.cv.notify()


class SimEngine(EngineBase):
    def __init__(
        self,
        kind: str = "call",
        name: str | None = None,
        latency_ms: float = 2.0,
        tail: float = 1.0,
        capacity: int = 8,
        max_batch: int = 16,
        seed: int = 0,
        item_error_rate: float = 0.0,
        outages: list[tuple[float, float]] | None = None,
        speed: float = 1.0,
        samples_ms: list[float] | None = None,
    ):
        super().__init__(name or f"sim-{kind}")
        self.kind = kind
        self.latency = latency_ms / 1000.0
        self.tail = tail
        self.cap = capacity
        self.max_batch = max_batch
        # One seeded generator per thread: delays on the submitting thread, injected
        # failures on the timer thread
        self.rng_delay = random.Random(f"{seed}/delay")
        self.rng_error = random.Random(f"{seed}/error")
        self.seed = seed
        self.item_error_rate = item_error_rate
        self.outages = outages or []
        self.speed = speed
        self.samples = [s / 1000.0 for s in samples_ms] if samples_ms else None
        self.clock: _Clock | None = None
        self.t0 = 0.0
        self.inflight = 0
        self.lock = threading.Lock()
        self.tickets: dict[int, tuple[list, Any]] = {}
        self.counts = {"submits": 0, "items": 0, "errors": 0, "outage_rejects": 0}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.clock is None:
            self.clock = _Clock(self.name)
            self.clock.start()
            self.t0 = time.monotonic()

    def stop(self) -> None:
        """Stop the clock; every pending ticket is answered with ``UnitError``."""
        if self.clock is None:
            return
        clock, self.clock = self.clock, None
        with self.lock:
            pending, self.tickets = self.tickets, {}
            self.inflight = 0
        clock.stop()
        for t, (items, done) in pending.items():
            clock.drop(t)
            done(t, [UnitError(EngineStopped(f"{self.name} stopped"))] * len(items))

    def in_outage(self) -> bool:
        t = time.monotonic() - self.t0
        return any(a <= t < b for a, b in self.outages)

    def ready(self) -> bool:
        return self.clock is not None and not self.in_outage()

    def spec(self) -> dict:
        return {"engine": "sim", "name": self.name, "kind": self.kind}

    def capacity(self) -> int:
        if self.in_outage():
            return 1
        with self.lock:
            free = self.cap - self.inflight
        return free if self.kind != "stream" else max(free, 0)

    def load(self) -> Load:
        with self.lock:
            return Load(running=self.inflight, waiting=0)

    # ------------------------------------------------------------ work
    def delay(self, item: Any) -> float:
        """Heavy-tailed, input-dependent latency with seeded jitter, or a replayed latency
        sample.
        """
        if self.samples:
            return self.rng_delay.choice(self.samples) / self.speed
        if self.latency == 0.0:
            return 0.0

        # The input hash sets the tail; a flat latency skips it
        h = 0.0
        if self.tail:
            h = int.from_bytes(hashlib.sha1(item.digest()).digest()[:4], "little") / 2**32
        base = self.latency * (1.0 + self.tail * (h**4) * 20.0)
        return base * (0.5 + self.rng_delay.random()) / self.speed

    def run_one(self, item: Any) -> Any:
        if self.item_error_rate and self.rng_error.random() < self.item_error_rate:
            with self.lock:
                self.counts["errors"] += 1
            return ItemError(SimError(f"injected failure in {item.node}"))
        try:
            payload = item.payload()
            raw = payload.run() if hasattr(payload, "run") else payload
            return Ok(item.finish(raw), True)
        except Exception as exc:  # noqa: BLE001
            return ItemError(exc)

    def submit(self, work: Any, done: Any) -> int:
        items = work if isinstance(work, list) else [work]
        with self.lock:
            self.counts["submits"] += 1
            self.counts["items"] += len(items)

        # Outage: the whole unit is blocked
        if self.in_outage():
            with self.lock:
                self.counts["outage_rejects"] += 1
            err = UnitError(SimOutage(f"{self.name} is down"))
            done(None, [err] * len(items))
            return -1

        due = time.monotonic() + max(self.delay(it) for it in items)
        ticket_box: dict = {}

        # Deliver: run every item when the submission is due
        def fire():
            with self.lock:
                if self.tickets.pop(ticket_box.get("t"), None) is None:
                    return
                self.inflight -= 1 if self.kind == "batch" else len(items)
            done(ticket_box.get("t"), [self.run_one(it) for it in items])

        # Timer, ticket and in-flight count registered together; fire waits for the lock
        with self.lock:
            clock = self.clock
            if clock is not None:
                self.inflight += 1 if self.kind == "batch" else len(items)
                t = clock.at(due, fire)
                ticket_box["t"] = t
                self.tickets[t] = (items, done)

        # A stopped engine answers every item at once
        if clock is None:
            done(None, [UnitError(EngineStopped(f"{self.name} stopped"))] * len(items))
            return -1
        return t

    def cancel(self, ticket: Any) -> None:
        if self.clock is None or ticket is None or ticket < 0:
            return
        if self.clock.drop(ticket):
            with self.lock:
                entry = self.tickets.pop(ticket, None)
                if entry is not None:
                    items, done = entry
                    self.inflight -= 1 if self.kind == "batch" else len(items)
            if entry is not None:
                done(ticket, [Cancelled()] * len(items))


def SimCall(**kw: Any) -> SimEngine:
    return SimEngine("call", **kw)


def SimBatch(**kw: Any) -> SimEngine:
    kw.setdefault("capacity", 2)
    return SimEngine("batch", **kw)


def SimStream(**kw: Any) -> SimEngine:
    kw.setdefault("capacity", 256)
    return SimEngine("stream", **kw)
