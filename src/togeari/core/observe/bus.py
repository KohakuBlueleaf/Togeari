"""The event bus: every transition is an event; subscribers get bounded queues (spec/08 §8.4).

The dispatcher only appends to subscriber queues; each subscriber drains its
queue on its own thread. Listeners are synchronous callbacks on the
dispatcher thread (cheap counting only), each for the kinds it registered.
Emitters ask ``wants(kind, job)`` first, so an event nobody receives is never
built.

``emit`` runs on the dispatcher thread, or while the machine is stopped: a
plugin's ``ctx.emit`` from another thread is posted to the dispatcher. ``counts``
and the listeners are therefore never touched by two threads at once.
"""

import contextlib
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("togeari.bus")

# Events delivered at every level, machine-wide and per subscriber (low rate); the rest is detail
CORE_EVENTS = {
    "job.admit",
    "job.end",
    "unit.up",
    "unit.down",
    "unit.attach",
    "unit.detach",
    "unit.detached",
    "lane.dead",
    "plugin.disabled",
    "snapshot.error",
    "fault.taken",
    "machine.start",
    "machine.stop",
    "machine.error",
    "counters",
}
LEVELS = ("all", "sampled", "counters")
OVERFLOW = ("drop", "sample", "counters_only")


@dataclass
class Event:
    kind: str
    t: float
    fields: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "t": self.t, **self.fields}


class _Sub:
    """One subscriber: its queue, its thread, and the level it receives now."""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.level = getattr(plugin, "events", "all") or "all"
        self.current = self.level
        self.overflow = getattr(plugin, "overflow", "drop")
        size = getattr(plugin, "queue_size", 10_000)
        self.q: queue.Queue = queue.Queue(maxsize=size)
        self.low = size // 4
        self.dropped = 0
        self.close_timeout_s = getattr(plugin, "close_timeout_s", 60.0)
        self.thread = threading.Thread(
            target=self.loop, name=f"togeari-sub-{getattr(plugin, 'name', 'plugin')}", daemon=True
        )
        self.thread.start()

    def loop(self) -> None:
        # Every queued event is delivered, up to the end marker
        while (ev := self.q.get()) is not None:
            try:
                self.plugin.on_event(ev)
            except Exception:
                log.warning(
                    "subscriber %s failed", getattr(self.plugin, "name", "?"), exc_info=True
                )

    def takes(self, core: bool, sampled: bool) -> bool:
        """Whether this subscriber receives an event at its current level."""
        if self.current != self.level and self.q.qsize() <= self.low:
            self.current = self.level
        match self.current:
            case "counters":
                return core
            case "sampled":
                return core or sampled
        return True

    def offer(self, ev: Event) -> None:
        """Queue ``ev``; a full queue drops it and lowers the level until it drains."""
        try:
            self.q.put_nowait(ev)
        except queue.Full:
            self.dropped += 1
            match self.overflow:
                case "counters_only":
                    self.current = "counters"
                case "sample":
                    self.current = "sampled"

    def close(self) -> None:
        """Deliver what is queued, then end the thread (each step bounded by
        ``close_timeout_s``)."""
        with contextlib.suppress(queue.Full):
            self.q.put(None, timeout=self.close_timeout_s)
        self.thread.join(timeout=self.close_timeout_s)

    def leave(self) -> None:
        """Drop what is queued (counted as dropped) and post the end marker, without waiting."""
        while True:
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                break
            if ev is not None:
                self.dropped += 1
        with contextlib.suppress(queue.Full):
            self.q.put_nowait(None)

    def join(self) -> None:
        self.thread.join(timeout=self.close_timeout_s)


class Bus:
    """Events by kind; the machine-wide detail level is the ``events`` knob of ``knobs``."""

    def __init__(self, knobs: dict):
        self.knobs = knobs
        self.subs: dict[int, _Sub] = {}
        # Subscribers detached without waiting; their threads are joined at close
        self.leaving: list[_Sub] = []
        self._sampled: dict[Any, bool] = {}
        self.counts: dict[str, int] = {}

        # Synchronous listeners on the dispatcher thread, fn(kind, fields), cheap: of every
        # kind (``listeners``) and of registered kinds (``by_kind``)
        self.listeners: list = []
        self.by_kind: dict[str, list] = {}

    @property
    def level(self) -> str:
        return self.knobs["events"]

    @property
    def active(self) -> bool:
        """True when anything receives some event."""
        return bool(self.subs or self.listeners or self.by_kind)

    # ------------------------------------------------------------ listeners
    def listen(self, fn: Any, kinds: Any = None) -> None:
        """Call ``fn(kind, fields)`` for each event of ``kinds`` (an iterable of kinds; None:
        every kind), whatever the levels."""
        if kinds is None:
            if fn not in self.listeners:
                self.listeners.append(fn)
            return
        for kind in kinds:
            fns = self.by_kind.setdefault(kind, [])
            if fn not in fns:
                fns.append(fn)

    def unlisten(self, fn: Any) -> None:
        """Remove ``fn`` from every kind it listens to."""
        if fn in self.listeners:
            self.listeners.remove(fn)
        for kind in list(self.by_kind):
            fns = self.by_kind[kind]
            if fn in fns:
                fns.remove(fn)
            if not fns:
                del self.by_kind[kind]

    # ------------------------------------------------------------ subscribers
    def subscribe(self, plugin: Any) -> None:
        if id(plugin) not in self.subs:
            self.subs[id(plugin)] = _Sub(plugin)

    def unsubscribe(self, plugin: Any, wait: bool = True) -> None:
        """End ``plugin``'s subscription. ``wait``: deliver its queued events and join its
        thread; otherwise drop them and return at once (the thread is joined at ``close``)."""
        sub = self.subs.pop(id(plugin), None)
        if sub is None:
            return
        if wait:
            sub.close()
            return
        sub.leave()
        self.leaving.append(sub)

    def sample(self, job: Any, sampled: bool) -> None:
        """Whether the job's detail events reach "sampled" subscribers."""
        self._sampled[job] = sampled

    def forget_job(self, job: Any) -> None:
        self._sampled.pop(job, None)

    # ------------------------------------------------------------ emitting
    def _to_subs(self, kind: str, job: Any) -> tuple[bool, bool] | None:
        """``(core, sampled)`` when the machine-wide level passes the event, else None."""
        if not self.subs:
            return None
        core = kind in CORE_EVENTS
        level = self.knobs["events"]
        if level == "counters" and not core:
            return None
        sampled = job is None or self._sampled.get(job, False)
        if level == "sampled" and not core and not sampled:
            return None
        return core, sampled

    def wants(self, kind: str, job: Any = None) -> bool:
        """Whether a listener or a subscriber receives an event of ``kind`` (of ``job``) now."""
        if self.listeners or kind in self.by_kind:
            return True
        passed = self._to_subs(kind, job)
        if passed is None:
            return False
        return any(sub.takes(*passed) for sub in list(self.subs.values()))

    def emit(self, kind: str, job: Any = None, **fields: Any) -> None:
        """Count the event and hand it to its listeners and to the subscribers that take it."""
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if job is not None:
            fields["job"] = job

        # Listeners of every kind and of this kind; a raising listener is removed
        fns = self.by_kind.get(kind)
        for fn in [*self.listeners, *fns] if fns else list(self.listeners):
            try:
                fn(kind, fields)
            except Exception:
                log.warning("bus listener %r failed; removed", fn, exc_info=True)
                self.unlisten(fn)

        # Machine-wide level, then each subscriber's own level
        passed = self._to_subs(kind, job)
        if passed is None:
            return
        ev = None
        for sub in list(self.subs.values()):
            if sub.takes(*passed):
                ev = ev or Event(kind, time.time(), fields)
                sub.offer(ev)

    def close(self) -> None:
        for sub in list(self.subs.values()):
            sub.close()
        self.subs.clear()

        # Subscribers that left without waiting end here
        leaving, self.leaving = self.leaving, []
        for sub in leaving:
            sub.join()

    def dropped(self) -> dict:
        return {getattr(s.plugin, "name", "?"): s.dropped for s in self.subs.values()}
