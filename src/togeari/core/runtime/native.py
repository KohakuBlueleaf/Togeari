"""The native dispatcher: ``togeari._rs.Core`` runs the cycle; this subclass holds the thread
loop, its supervision and the queues other threads touch (spec/11 §11.3).

Work items name their statement through a ``togeari._rs.Owner`` (its ``job`` is a ``JobView``);
policies written in Python see ``ReadyView``s of ``ReadyInf``s; plugins and queries read jobs
through ``JobView``s.
"""

import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from typing import Any

from togeari.core.contracts.io import JobRecord
from togeari.core.runtime import collector
from togeari.core.runtime.handles import DispatcherDied, RunState
from togeari.core.runtime.loop import _settle
from togeari.core.runtime.pool.signals import Signals

try:
    from togeari import _rs
except ImportError:
    _rs = None

log = logging.getLogger("togeari.dispatcher")


class _JobRef:
    __slots__ = ("id", "key", "entry")

    def __init__(self, id: int, key: str, entry: Any):
        self.id = id
        self.key = key
        self.entry = entry


class ReadyInf:
    """A ready statement as ``ReadyView`` reads it: ``job`` (id, key, entry), ``instr``, ``key``."""

    __slots__ = ("job", "instr", "key")

    def __init__(self, job: int, key: str, entry: Any, instr: Any, order_key: tuple):
        self.job = _JobRef(job, key, entry)
        self.instr = instr
        self.key = order_key


class InfView:
    __slots__ = ("instr", "key", "state", "item")

    def __init__(self, instr: Any, key: tuple, state: str, item: Any):
        self.instr = instr
        self.key = key
        self.state = state
        self.item = item


class StrandView:
    __slots__ = ("id", "state", "pc", "key", "spawn")

    def __init__(self, id: int, state: str, pc: int, key: tuple, spawner: bool):
        self.id = id
        self.state = state
        self.pc = pc
        self.key = key
        self.spawn = True if spawner else None

    def next_key(self) -> tuple:
        return self.key


class JobView:
    """A resident job of the native core, read live; a job that ended reads as empty."""

    __slots__ = ("_d", "id")

    def __init__(self, d: Any, id: int):
        self._d = d
        self.id = id

    def _info(self, i: int, default: Any) -> Any:
        info = self._d._job_info(self.id)
        return default if info is None else info[i]

    @property
    def key(self) -> str:
        return self._info(0, None)

    @property
    def entry(self) -> Any:
        return self._info(1, None)

    @property
    def inputs(self) -> dict:
        return self._info(2, {})

    @property
    def paused(self) -> bool:
        return self._info(3, False)

    @property
    def issued(self) -> int:
        return self._info(4, 0)

    @property
    def unfinished(self) -> int:
        return self._info(5, 0)

    @property
    def runnable(self) -> list:
        return [None] * self._info(6, 0)

    @property
    def stats(self) -> dict:
        return self._info(7, {})

    @property
    def held(self) -> list:
        return [None] * self._info(8, 0)

    @property
    def ended(self) -> bool:
        return self._d._job_info(self.id) is None

    @property
    def window(self) -> list[InfView]:
        return [InfView(*row) for row in self._d._job_window(self.id)]

    @property
    def strands(self) -> dict[int, StrandView]:
        return {row[0]: StrandView(*row) for row in self._d._job_strands(self.id)}

    def slot_counts(self) -> tuple[int, int]:
        return self._d._slot_counts(self.id)

    def __repr__(self) -> str:
        return f"<job {self.id} {self.key!r}>"


_Base = _rs.Core if _rs is not None and getattr(_rs, "DISPATCHER", False) else object


class NativeDispatcher(_Base):
    """The dispatcher of ``Machine(core="rust")``: the same attributes and methods the machine,
    plugins and queries use on the Python ``Dispatcher``."""

    def __init__(self, machine: Any, seed: int = 0):
        self.m = machine
        self.code = machine.code
        self.knobs = machine.knobs
        self.plugins = machine.plugins
        self.bus = machine.bus
        self.store = machine.store
        self.lanes = machine.lanes
        self.inbox = _rs.Inbox()
        self._bind(self.inbox)

        # Source and sink of the current run
        self.run: RunState | None = None
        self.source_buf: queue.Queue | None = None
        self.source_want = False
        self.sink_q: queue.Queue | None = None
        self.sink_pending: deque = deque()
        self.sink_wait = False
        self.on_idle = threading.Event()

        # Run state
        self.running = False
        self.stopping = False
        self.dead: BaseException | None = None
        self.death_lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.started_at = 0.0
        self.gc_owner: int | None = None
        self.gc_set: tuple | None = None

    def on_thread(self) -> bool:
        return self.thread is not None and threading.current_thread() is self.thread

    # ------------------------------------------------------------ lane hooks (any thread)
    def lane_done(self, lane: Any, sub: Any, results: list) -> None:
        self.inbox.put(("done", lane, sub, list(results)))

    def lane_woke(self, lane: Any) -> None:
        self.inbox.put(("call", lane.capacity_rose))

    # ------------------------------------------------------------ queries, runs, the sink
    def answer(self, table: dict, kind: str, name: str, args: dict, reply: Any) -> None:
        """Run a query or command; its result, its later result (a Future) or its error answers."""
        fn = table.get(name)
        if fn is None:
            reply._set(error=KeyError(f"no {kind} {name!r}"))
            return
        try:
            value = fn(self.m, **args)
        except Exception as exc:  # noqa: BLE001
            reply._set(error=exc)
            return
        if isinstance(value, Future):
            value.add_done_callback(lambda f: _settle(reply, f))
        else:
            reply._set(value)

    def attach_run(self, buf: queue.Queue, sink_q: queue.Queue, run: RunState) -> None:
        self.source_buf = buf
        self.sink_q = sink_q
        self.run = run

    def release_source(self) -> None:
        """Forget the run's source buffer, emptying it so a blocked reader gets through."""
        buf, self.source_buf = self.source_buf, None
        while buf is not None:
            try:
                buf.get_nowait()
            except queue.Empty:
                break

    def take_source(self) -> Any:
        """The next buffered source item, or None; after the last one the buffer is let go."""
        buf = self.source_buf
        try:
            return buf.get_nowait()
        except queue.Empty:
            pass

        # The reader wakes the dispatcher after its next put, or after its last
        self.source_want = True
        finished = self.run is not None and self.run.source_finished
        try:
            return buf.get_nowait()
        except queue.Empty:
            if finished:
                self.source_buf = None
            return None

    def to_sink(self, rec: JobRecord) -> None:
        """Hand a record to the sink writer; a full queue holds it here (and pauses admission)."""
        if not self.sink_pending:
            try:
                self.sink_q.put_nowait(rec)
                return
            except queue.Full:
                pass
        self.sink_pending.append(rec)
        self.flush_sink()

    def flush_sink(self) -> None:
        while self.sink_pending:
            try:
                self.sink_q.put_nowait(self.sink_pending[0])
            except queue.Full:
                # Full: the writer wakes the dispatcher after its next take; one more try
                self.sink_wait = True
                try:
                    self.sink_q.put_nowait(self.sink_pending[0])
                except queue.Full:
                    return
            self.sink_pending.popleft()

    def signals(self) -> dict:
        return Signals(self).to_dict()

    # ------------------------------------------------------------ the collector
    def apply_collector(self) -> None:
        """The ``gc_threshold`` knob, asked of the shared process collector when it changes."""
        want = self.knobs["gc_threshold"]
        if want == self.gc_set or self.dead is not None:
            return
        self.gc_set = want
        collector.ask(self.gc_owner, want)

    def withdraw_collector(self) -> None:
        self.gc_set = None
        collector.ask(self.gc_owner, None)

    # ------------------------------------------------------------ supervision
    def loop(self) -> None:
        self.running = True
        self.started_at = time.perf_counter()
        self.gc_set = None
        self.gc_owner = next(collector.owners)
        try:
            while self.running:
                self.cycle()
                self.after_cycle()
        except BaseException as exc:  # noqa: BLE001
            self.died(exc)
        finally:
            self.withdraw_collector()
            self.release_source()
            if self.run is not None:
                self.run.done.set()
                self.run = None

    def died(self, exc: BaseException) -> None:
        """Answer everyone: resident jobs fail, posted work is refused, the source is let go."""
        with self.death_lock:
            first = self.dead is None
            if first:
                self.dead = exc
        if not first:
            self.refuse_inbox()
            return
        log.critical("dispatcher stopped on an error", exc_info=exc)
        self.running = False
        self.bus.emit("machine.error", error=f"{type(exc).__name__}: {exc}")
        failure = {
            "kind": "DispatcherDied",
            "node": None,
            "message": f"{type(exc).__name__}: {exc}",
        }
        for key, entry, outputs, handle in self.drain_dead():
            self.answer_dead(JobRecord(key, entry, False, {}, failure, list(outputs)), handle)

        # The run in progress ends now, even while the loop thread is still stuck
        run = self.run
        if run is not None:
            run.stopped = True
            run.done.set()
        self.refuse_inbox()

    def abandon(self, exc: BaseException) -> None:
        self.died(exc)
        if self.gc_owner is not None:
            self.withdraw_collector()

    def answer_dead(self, rec: JobRecord, handle: Any) -> None:
        if handle is not None:
            handle._set(rec)
        elif self.sink_q is not None:
            try:
                self.sink_q.put_nowait(rec)
            except queue.Full:
                log.error("record of job %s lost: the sink queue is full", rec.key)

    def refuse_inbox(self) -> None:
        """After death: every posted submit, query, command and run gets the error."""
        err = DispatcherDied(f"dispatcher died: {self.dead!r}")
        failure = {"kind": "DispatcherDied", "node": None, "message": str(err)}
        while True:
            try:
                ev = self.inbox.get_nowait()
            except queue.Empty:
                return
            match ev:
                case ("submit", item, handle):
                    rec = JobRecord(item.key, item.entry or "", False, {}, failure)
                    self.answer_dead(rec, handle)
                case ("query" | "command", _, _, reply) | ("stop", _, reply):
                    reply._set(error=err)
                case ("run", _, _, run):
                    run.done.set()
