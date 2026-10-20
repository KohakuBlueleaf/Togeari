"""The deterministic driver: one machine, no dispatcher or engine threads, a virtual clock.

``run_trace(machine_factory, jobs, seed)`` builds the machine, binds its scripted engines to
one schedule, sets the virtual clock on the dispatcher and every lane, and feeds every job
through the run path (source buffer, admission, sink queue). It then steps the dispatcher
cycle by cycle on the calling thread. Between cycles, when nothing is ready, the clock moves to
the next engine answer or lane deadline, and the answers due then fire.

The collector is off while a trace runs, so values are freed by reference count only, at
points the program fixes.
"""

import gc
import queue
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple

from togeari.core.contracts.io import JobRecord, SourceItem
from togeari.core.runtime import collector
from togeari.core.runtime.handles import DispatcherDied, RunState
from togeari.testing.trace.decisions import DecisionLog
from togeari.testing.trace.engines import Schedule, ScriptedEngine, VirtualClock


class TraceStuck(RuntimeError):
    """The machine neither finished nor had anything left to wait for, or ran too long."""


class Trace(NamedTuple):
    """The decisions and records of one run, its cycles and virtual seconds, and the (stopped)
    machine, whose code memory conformance reads."""

    log: DecisionLog
    records: list[JobRecord]
    cycles: int
    clock_s: float
    machine: Any


def scripted_units(machine: Any) -> list[ScriptedEngine]:
    """Every unit of every lane; TypeError for a unit that is not scripted."""
    units = [u for lane in machine.lanes for u in lane.units]
    other = sorted({type(u).__name__ for u in units if not isinstance(u, ScriptedEngine)})
    if other:
        raise TypeError(f"the trace driver runs scripted engines only, not {', '.join(other)}")
    return units


def set_clock(machine: Any, clock: Callable[[], float]) -> None:
    """The dispatcher's and every lane's scheduling clock."""
    machine.dispatcher.clock = clock
    for lane in machine.lanes:
        lane.clock = clock


def as_items(jobs: Iterable) -> list[SourceItem]:
    """Source items; a plain dict becomes job ``str(i)``."""
    return [
        j if isinstance(j, SourceItem) else SourceItem(str(i), dict(j), auto_key=True)
        for i, j in enumerate(jobs)
    ]


def run_trace(
    machine_factory: Callable[[], Any],
    jobs: Iterable,
    seed: int = 0,
    max_cycles: int = 1_000_000,
) -> Trace:
    """Run ``jobs`` on ``machine_factory()`` deterministically; the decision log and records.

    The same factory, jobs and seed give the same trace in any process.
    """
    m = machine_factory()
    log = DecisionLog()
    try:
        clock = VirtualClock()
        schedule = Schedule(clock)
        for unit in scripted_units(m):
            unit.bind(schedule, seed)
        set_clock(m, clock)
        log.attach(m)
        records, cycles = _drive(m, as_items(jobs), schedule, max_cycles)
    finally:
        log.detach()
        m.stop(drain=False)
    return Trace(log, records, cycles, clock(), m)


def _drive(
    m: Any, items: list[SourceItem], schedule: Schedule, max_cycles: int
) -> tuple[list[JobRecord], int]:
    d = m.dispatcher
    clock = schedule.clock

    # The run: every job buffered, the source finished, an unbounded sink queue
    buf: queue.Queue = queue.Queue()
    for it in items:
        buf.put(it)
    sink_q: queue.Queue = queue.Queue()
    run = RunState()
    run.source_finished = True
    d.inbox.put(("run", buf, sink_q, run))

    # What `loop` sets up, on this thread
    m.plugins.notify("on_machine_start")
    m.bus.emit("machine.start")
    d.running = True
    d.gc_set = None
    d.gc_owner = next(collector.owners)
    collecting = gc.isenabled()
    gc.disable()
    cycles = 0
    try:
        while not run.done.is_set():
            if d.dead is not None:
                raise DispatcherDied(f"dispatcher died: {d.dead!r}") from d.dead
            if d.inbox.empty() and not (d.dirty or d.ready):
                _advance(d, schedule, clock)
            if d.inbox.empty():
                d.inbox.put(("wake",))
            d.cycle()
            d.after_cycle()
            cycles += 1
            if cycles > max_cycles:
                raise TraceStuck(f"no end after {max_cycles} cycles: {_state(d, schedule)}")
    finally:
        d.withdraw_collector()
        d.running = False
        if collecting:
            gc.enable()

    records = []
    while True:
        try:
            records.append(sink_q.get_nowait())
        except queue.Empty:
            return records, cycles


def _advance(d: Any, schedule: Schedule, clock: VirtualClock) -> None:
    """Move the clock to the next engine answer or lane deadline; fire the answers due."""
    targets = []
    due = schedule.next_due()
    if due is not None:
        targets.append(due)
    timeout = d.next_timeout()
    if timeout is not None:
        targets.append(clock() + timeout)
    if not targets:
        raise TraceStuck(f"nothing to wait for: {_state(d, schedule)}")
    clock.advance_to(min(targets))
    schedule.fire_due()


def _state(d: Any, schedule: Schedule) -> str:
    lanes = {lane.name: lane.signals() for lane in d.lanes}
    return f"jobs {len(d.jobs)}, pending answers {len(schedule.pending)}, lanes {lanes}"
