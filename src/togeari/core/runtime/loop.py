"""The dispatcher loop: inbox, fetch, issue, pump, retire, admit; supervised (spec/03 §3.11).

The loop blocks on the inbox until an event arrives or the nearest lane
deadline passes; engines, the source reader and the sink writer post the
events. An error while working on one job fails that job; an error in one
event's handling is logged and counted in ``machine_errors``; any other error
stops the loop, and every waiting caller is answered with ``DispatcherDied``.
"""

import logging
import queue
import time
from concurrent.futures import Future
from typing import Any

from togeari.core.contracts.io import JobRecord
from togeari.core.contracts.results import ItemError
from togeari.core.runtime import collector
from togeari.core.runtime.handles import DispatcherDied, RunState

log = logging.getLogger("togeari.dispatcher")

# Cycles between two `dispatcher.cycle` events
CYCLE_EVENT_EVERY = 100


class LoopStage:
    """The cycle and its supervision (mixed into ``Dispatcher``)."""

    # The collector ask: its owner number once the loop runs, and the threshold asked
    gc_owner: int | None = None
    gc_set: tuple | None = None

    # ------------------------------------------------------------ waiting
    def next_timeout(self) -> float | None:
        """0 with work ready now; the time to the nearest lane deadline; None to wait for events."""
        if self.dirty or self.ready:
            return 0.0
        now = self.clock()
        nearest = None
        for lane in self.lanes:
            d = lane.next_deadline(now)
            if d is not None and (nearest is None or d < nearest):
                nearest = d
        if nearest is None:
            return None
        return max(0.0, nearest - now)

    def next_event(self, timeout: float | None) -> Any:
        try:
            if timeout is None:
                return self.inbox.get()
            if timeout > 0:
                return self.inbox.get(timeout=timeout)
            return self.inbox.get_nowait()
        except queue.Empty:
            return None

    # ------------------------------------------------------------ events
    def handle(self, ev: tuple) -> None:
        match ev:
            # Results from a lane
            case ("done", lane, sub, results):
                try:
                    settled = lane.results(sub, results, self.clock())
                except Exception as exc:  # noqa: BLE001
                    self.event_error("done", exc)
                    settled = [(it, ItemError(exc)) for it in sub.items]
                    for it, _ in settled:
                        it.ext["settled"] = True
                for it, r in settled:
                    self.deliver(it, r, sub.unit)

            # Requests from other threads: jobs, queries, commands, calls
            case ("submit", item, handle):
                try:
                    self.start_job(item, handle)
                except Exception as exc:
                    log.error("job %s could not start", item.key, exc_info=exc)
                    self.stats["machine_errors"] += 1
                    self.reject(item, "MachineError", f"{type(exc).__name__}: {exc}", handle)
            case ("query", name, args, reply):
                self.answer(self.m.queries, "query", name, args, reply)
            case ("command", name, args, reply):
                self.answer(self.m.commands, "command", name, args, reply)
            case ("call", fn) | ("call", fn, None):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    self.event_error("call", exc)
            case ("call", fn, owner):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    self.plugins.failed_by_name(owner, "post", exc)

            # Runs, wake-ups and stop
            case ("run", buf, sink_q, run):
                self.attach_run(buf, sink_q, run)
            case ("wake",):
                pass
            case ("stop", drain, reply):
                self.begin_stop(drain)
                reply._set(True)

    def event_error(self, kind: str, exc: BaseException) -> None:
        """An inbox event whose handling raised: logged and counted; the loop goes on."""
        log.error("dispatcher event %s failed", kind, exc_info=exc)
        self.stats["machine_errors"] += 1

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

    # ------------------------------------------------------------ the collector
    def apply_collector(self) -> None:
        """The ``gc_threshold`` knob, asked of the shared process collector when it changes."""
        want = self.knobs["gc_threshold"]
        if want == self.gc_set or self.dead is not None:
            return
        self.gc_set = want
        collector.ask(self.gc_owner, want)

    def withdraw_collector(self) -> None:
        """The loop's collector ask is withdrawn (no-op when it holds none)."""
        self.gc_set = None
        collector.ask(self.gc_owner, None)

    # ------------------------------------------------------------ the cycle
    def cycle(self) -> None:
        phase = self.stats["phase_s"]
        self.apply_collector()

        # Inbox: wait for the first event (idle time), then drain the rest
        t0 = time.perf_counter()
        timeout = self.next_timeout()
        busy = time.perf_counter() - t0
        ev = self.next_event(timeout)
        t_cycle = time.perf_counter()
        if ev is not None:
            self.handle(ev)
        while True:
            try:
                ev = self.inbox.get_nowait()
            except queue.Empty:
                break
            self.handle(ev)
        if self.sink_pending:
            self.flush_sink()
        t1 = time.perf_counter()
        phase["drain"] = phase.get("drain", 0.0) + t1 - t_cycle

        # Fetch for jobs with runnable strands
        dirty, self.dirty = self.dirty, {}
        for jid in dirty:
            job = self.jobs.get(jid)
            if job is not None and not job.ended and not job.paused:
                try:
                    self.front.advance(job)
                except Exception as exc:  # noqa: BLE001
                    self.job_error(job, exc)
        t2 = time.perf_counter()
        phase["fetch"] = phase.get("fetch", 0.0) + t2 - t1

        # Issue ready statements, then multi-issue to units
        self.issue()
        t3 = time.perf_counter()
        phase["issue"] = phase.get("issue", 0.0) + t3 - t2
        now = self.clock()
        for lane in self.lanes:
            lane.pump(now)
            for it, r in lane.timers(now):
                self.deliver(it, r)
        t4 = time.perf_counter()
        phase["pump"] = phase.get("pump", 0.0) + t4 - t3

        # Retire in program order; finished jobs go to their handles or the sink
        for jid in dirty:
            job = self.jobs.get(jid)
            if job is not None and not job.ended:
                try:
                    self.retire(job)
                except Exception as exc:  # noqa: BLE001
                    self.job_error(job, exc)
        t5 = time.perf_counter()
        phase["retire"] = phase.get("retire", 0.0) + t5 - t4

        # Admission
        self.admit()
        t6 = time.perf_counter()
        phase["admit"] = phase.get("admit", 0.0) + t6 - t5

        # Cycle totals and time histogram: bucket = bit length of the cycle's microseconds
        stats = self.stats
        stats["cycles"] += 1
        dt = t6 - t_cycle
        stats["busy_s"] += busy + dt
        bucket = max(0, int(dt * 1e6)).bit_length()
        hist = stats["cycle_hist"]
        hist[bucket] = hist.get(bucket, 0) + 1
        if dt > stats["cycle_max_s"]:
            stats["cycle_max_s"] = dt
        if stats["cycles"] % CYCLE_EVENT_EVERY == 0 and self.bus.wants("dispatcher.cycle"):
            self.bus.emit("dispatcher.cycle", cycles=stats["cycles"], phase_s=dict(phase))

    # ------------------------------------------------------------ runs and stop
    def attach_run(self, buf: queue.Queue, sink_q: queue.Queue, run: RunState) -> None:
        self.source_buf = buf
        self.sink_q = sink_q
        self.run = run

    def begin_stop(self, drain: bool) -> None:
        """Stop admitting; with ``drain=False`` cancel every resident job now."""
        self.stopping = True
        if self.run is not None:
            self.run.stopped = True
        if not drain:
            for job in list(self.jobs.values()):
                self.cancel_job(job, "the machine stopped")

    def after_cycle(self) -> None:
        """Run done, stop done, idle flag."""
        settled = not self.jobs and not self.sink_pending
        idle = settled and self.source_buf is None
        if self.run is not None and (idle or (self.stopping and settled)):
            self.release_source()
            self.run.done.set()
            self.run = None
        if self.stopping and settled:
            self.running = False
        if idle and self.inbox.empty():
            self.on_idle.set()
        else:
            self.on_idle.clear()

    def release_source(self) -> None:
        """Forget the run's source buffer, emptying it so a blocked reader gets through."""
        buf, self.source_buf = self.source_buf, None
        while buf is not None:
            try:
                buf.get_nowait()
            except queue.Empty:
                break

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
        """Answer everyone: resident jobs fail, posted work is refused, the source is let go.

        Runs once, even when the loop thread and an abandoning thread call it at once; a later
        call only refuses what was posted since.
        """
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
        for job in list(self.jobs.values()):
            job.ended = True
            rec = JobRecord(
                job.key, job.entry.name, False, {}, failure, list(job.entry.outputs or [])
            )
            self.answer_dead(rec, job.handle)
        self.jobs.clear()

        # The run in progress ends now, even while the loop thread is still stuck
        run = self.run
        if run is not None:
            run.stopped = True
            run.done.set()
        self.refuse_inbox()

    def abandon(self, exc: BaseException) -> None:
        """From another thread, for a loop that did not stop in time: it is dead from now on
        (``died``); whatever it is running ends without effect on any job, and its collector ask
        is withdrawn."""
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
        """After death: every posted submit, query, command and run gets the error (from any
        thread)."""
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


def _settle(reply: Any, fut: Future) -> None:
    exc = fut.exception()
    if exc is not None:
        reply._set(error=exc)
    else:
        reply._set(fut.result())
