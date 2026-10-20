"""Load generators, each on its own thread, logging every completion with its time.

- ``UnitFeeder`` submits straight to one engine unit and keeps it full: as many
  tickets as ``capacity()`` allows, up to ``max_inflight``;
- ``JobFeeder`` submits jobs of one entry to a machine, ``inflight`` at a time,
  optionally paced at ``rate`` jobs per second.

``window(t0, t1)`` reports what completed between two ``perf_counter`` times.
"""

import itertools
import threading
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from s1.common.stats import percentiles, scaled
from togeari import Ok, SourceItem
from togeari.core.runtime.dispatcher import JobHandle


class _Log:
    """Completions as (time, latency, items, items ok), plus error kinds and one sample each."""

    def __init__(self) -> None:
        self.rows: list[tuple[float, float, int, int]] = []
        self.errors: Counter = Counter()
        self.samples: dict[str, str] = {}

    def add(self, t0: float, n: int, ok: int, errors: list[Any]) -> None:
        t1 = time.perf_counter()
        self.rows.append((t1, t1 - t0, n, ok))
        for err in errors:
            kind = type(err).__name__
            self.errors[kind] += 1
            self.samples.setdefault(kind, repr(err)[:500])

    def window(self, t0: float, t1: float) -> dict:
        inside = [r for r in self.rows if t0 <= r[0] <= t1]
        wall = t1 - t0
        items = sum(r[2] for r in inside)
        ok = sum(r[3] for r in inside)
        return {
            "wall_s": wall,
            "tickets": len(inside),
            "items": items,
            "items_ok": ok,
            "items_per_s": items / wall if wall > 0 else 0.0,
            "items_ok_per_s": ok / wall if wall > 0 else 0.0,
            "latency_ms": scaled(percentiles([r[1] for r in inside]), 1000.0),
            "errors_total": dict(self.errors),
            "error_samples": dict(self.samples),
        }


class UnitFeeder:
    """Keeps ``unit`` full with work from ``make(i)``: one item per call unit, ``batch`` per
    batch unit.
    """

    def __init__(
        self,
        unit: Any,
        make: Callable[[int], Any],
        batch: int | None = None,
        max_inflight: int = 1 << 30,
        poll_s: float = 0.002,
        name: str = "unit",
    ):
        self.unit = unit
        self.make = make
        self.batch = batch
        self.max_inflight = max_inflight
        self.poll_s = poll_s
        self.name = name
        self.log = _Log()
        self.count = itertools.count()
        self.cv = threading.Condition()
        self.inflight = 0
        self.kick = False
        self.halt = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.halt.clear()
        self.thread = threading.Thread(target=self._loop, name=f"feed-{self.name}", daemon=True)
        self.thread.start()

    def _work(self) -> tuple[Any, int]:
        match self.batch:
            case None:
                return self.make(next(self.count)), 1
            case n:
                return [self.make(next(self.count)) for _ in range(n)], n

    def _loop(self) -> None:
        while not self.halt.is_set():
            # Submit while the unit has room
            if self.inflight < self.max_inflight and self.unit.capacity() > 0:
                work, n = self._work()
                with self.cv:
                    self.inflight += 1
                t0 = time.perf_counter()
                self.unit.submit(
                    work, lambda ticket, results, t0=t0, n=n: self._done(t0, n, results)
                )
                continue

            # Wait for a completion (or poll again: a token bucket refills without one)
            with self.cv:
                if not self.kick:
                    self.cv.wait(self.poll_s)
                self.kick = False

    def _done(self, t0: float, n: int, results: list) -> None:
        bad = [r for r in results if not isinstance(r, Ok)]
        self.log.add(t0, n, n - len(bad), [getattr(r, "error", r) for r in bad])
        with self.cv:
            self.inflight -= 1
            self.kick = True
            self.cv.notify_all()

    def stop(self, drain_s: float = 60.0) -> int:
        """Stop submitting and wait for in-flight tickets; returns those still in flight."""
        self.halt.set()
        if self.thread is not None:
            self.thread.join(drain_s)
        deadline = time.monotonic() + drain_s
        with self.cv:
            while self.inflight > 0 and time.monotonic() < deadline:
                self.cv.wait(0.05)
            return self.inflight

    def window(self, t0: float, t1: float) -> dict:
        return {"feeder": self.name, "kind": "direct", **self.log.window(t0, t1)}


class _Handle(JobHandle):
    """A job handle that reports its record to the feeder (on the dispatcher thread)."""

    def __init__(self, key: str, t0: float, done: Callable[["_Handle", Any], None]):
        super().__init__(key)
        self.t0 = t0
        self.done_cb = done

    def _set(self, record: Any) -> None:
        super()._set(record)
        self.done_cb(self, record)


class JobFeeder:
    """Submits ``entry`` jobs with inputs ``make(i)``: ``inflight`` at most, ``rate`` per second."""

    def __init__(
        self,
        machine: Any,
        entry: str,
        make: Callable[[int], dict],
        inflight: int,
        rate: float | None = None,
        name: str | None = None,
    ):
        self.machine = machine
        self.entry = entry
        self.make = make
        self.inflight = inflight
        self.rate = rate
        self.name = name or entry
        self.log = _Log()
        self.count = itertools.count()
        self.slots = threading.Semaphore(inflight)
        self.halt = threading.Event()
        self.thread: threading.Thread | None = None
        self.late = 0

    def start(self) -> None:
        self.halt.clear()
        self.thread = threading.Thread(target=self._loop, name=f"jobs-{self.name}", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        inbox = self.machine.dispatcher.inbox
        interval = 1.0 / self.rate if self.rate else 0.0
        next_t = time.perf_counter()
        while not self.halt.is_set():
            # Pacing: wait for the next slot; a schedule more than a second behind restarts
            if interval:
                now = time.perf_counter()
                if next_t > now:
                    self.halt.wait(next_t - now)
                elif now - next_t > 1.0:
                    self.late += 1
                    next_t = now
                next_t += interval

            if not self.slots.acquire(timeout=0.05):
                continue
            i = next(self.count)
            key = f"{self.name}-{i}"
            handle = _Handle(key, time.perf_counter(), self._done)
            inbox.put(("submit", SourceItem(key, self.make(i), self.entry), handle))

    def _done(self, handle: _Handle, record: Any) -> None:
        errors = [] if record.ok else [RuntimeError(str(record.failure))]
        self.log.add(handle.t0, 1, 1 if record.ok else 0, errors)
        self.slots.release()

    def stop(self, drain_s: float = 60.0) -> int:
        """Stop submitting and wait for running jobs; returns those still running."""
        self.halt.set()
        if self.thread is not None:
            self.thread.join(drain_s)
        got = 0
        deadline = time.monotonic() + drain_s
        while got < self.inflight and time.monotonic() < deadline:
            if self.slots.acquire(timeout=0.05):
                got += 1
        for _ in range(got):
            self.slots.release()
        return self.inflight - got

    def window(self, t0: float, t1: float) -> dict:
        return {
            "feeder": self.name,
            "kind": "jobs",
            "rate": self.rate,
            "late_resets": self.late,
            **self.log.window(t0, t1),
        }
