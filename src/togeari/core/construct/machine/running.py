"""Running: the dispatcher thread, runs over a source and a sink, jobs, queries, commands, stop."""

import contextlib
import itertools
import queue
import threading
import time
from typing import Any

from togeari.core.construct.units import stop_all
from togeari.core.contracts.io import SourceItem
from togeari.core.runtime.handles import SINK_END, DispatcherDied, JobHandle, Reply, RunState

_keys = itertools.count(1)

# Seconds a stop without drain waits for the dispatcher, at most
STOP_CANCEL_S = 10.0

# Seconds added to the start timeout when waiting for a `load` or `attach` reply
REPLY_SLACK_S = 30.0


class RunStage:
    """The machine's running interface (mixed into ``Machine``)."""

    # ------------------------------------------------------------ the dispatcher thread
    def _ensure_thread(self) -> None:
        d = self.dispatcher
        if d.dead is not None:
            raise DispatcherDied(f"dispatcher died: {d.dead!r}") from d.dead
        if self._stopped or d.stopping:
            raise RuntimeError("the machine is stopped")
        if not self.units_started and any(lane.units for lane in self.lanes):
            raise RuntimeError("the machine was built without starting its units")
        if d.thread is None:
            self.plugins.notify("on_machine_start")
            self.bus.emit("machine.start")
            d.thread = threading.Thread(target=d.loop, name="togeari-dispatcher", daemon=True)
            d.thread.start()

    def _post(self, ev: tuple) -> None:
        """Post to the inbox; after the dispatcher died, the event is answered with the error."""
        d = self.dispatcher
        d.inbox.put(ev)
        if d.dead is not None:
            d.refuse_inbox()

    # ------------------------------------------------------------ runs
    def run(self, source: Any, sink: Any, limit: int | None = None) -> dict:
        """Run until the source is drained and every job has ended; return a summary.

        Raises ``DispatcherDied`` when the dispatcher died, and the source's or
        the sink's own error when either failed (after the run ends).
        """
        with self._run_lock:
            self._ensure_thread()
            d = self.dispatcher
            buf: queue.Queue = queue.Queue(maxsize=self.source_buffer)
            sink_q: queue.Queue = queue.Queue(maxsize=self.sink_buffer)
            run = RunState()
            t0 = time.perf_counter()

            # Reader and writer threads, then the run posted to the dispatcher
            reader = threading.Thread(
                target=_read, args=(d, source, limit, buf, run), name="togeari-source", daemon=True
            )
            writer = threading.Thread(
                target=_write, args=(d, sink, sink_q, run), name="togeari-sink", daemon=True
            )
            writer.start()
            self._post(("run", buf, sink_q, run))
            reader.start()

            # Wait for the dispatcher to finish the run (or die); after a death nobody reads
            # the buffer, so it is emptied until the reader stops
            run.done.wait()
            while reader.is_alive():
                if d.dead is not None:
                    with contextlib.suppress(queue.Empty):
                        while True:
                            buf.get_nowait()
                reader.join(0.05)

            # Close the sink, then report
            sink_q.put(SINK_END)
            writer.join()
            if hasattr(sink, "close"):
                sink.close()
            if d.dead is not None:
                raise DispatcherDied(f"dispatcher died: {d.dead!r}") from d.dead
            if run.source_error is not None:
                raise run.source_error
            if run.sink_error is not None:
                raise run.sink_error
            return self.summary(time.perf_counter() - t0, stopped=run.stopped)

    def summary(self, seconds: float, stopped: bool = False) -> dict:
        s = self.dispatcher.stats
        jobs = s["jobs_ok"] + s["jobs_failed"]
        return {
            "seconds": seconds,
            "jobs": jobs,
            "jobs_ok": s["jobs_ok"],
            "jobs_failed": s["jobs_failed"],
            "jobs_per_s": jobs / seconds if seconds > 0 else 0.0,
            "calls": s["calls"],
            "squashed": s["squashed"],
            "wasted": s["wasted"],
            "store_hits": s["store_hits"],
            "machine_errors": s["machine_errors"],
            "stopped": stopped,
            "dispatcher_busy": s["busy_s"] / seconds if seconds > 0 else 0.0,
            "lanes": {
                lane.name: {**lane.stats, "batch_sizes": dict(lane.batch_sizes)}
                for lane in self.lanes
            },
        }

    # ------------------------------------------------------------ jobs, queries, commands
    def submit(self, inputs: dict, key: str | None = None, entry: str | None = None) -> JobHandle:
        auto = key is None
        key = key or f"job-{next(_keys)}"
        h = JobHandle(key)
        self._ensure_thread()
        self._post(("submit", SourceItem(key, dict(inputs), entry, auto_key=auto), h))
        return h

    def inspect(self, query: str, /, **args: Any) -> Reply:
        r = Reply()
        self._ensure_thread()
        self._post(("query", query, args, r))
        return r

    def command(self, cmd: str, /, **args: Any) -> Reply:
        r = Reply()
        self._ensure_thread()
        self._post(("command", cmd, args, r))
        return r

    def load(self, program: Any, entries: list) -> list[str]:
        reply = self.command("load", program=program, entries=entries)
        return reply.result(self.start_timeout_s + REPLY_SLACK_S)

    def attach(self, lane: Any, unit: Any) -> str:
        name = lane if isinstance(lane, str) else lane.name
        reply = self.command("attach", lane=name, unit=unit)
        return reply.result(self.start_timeout_s + REPLY_SLACK_S)

    def detach(self, unit: Any, drain: bool = True) -> int:
        name = unit if isinstance(unit, str) else unit.name
        return self.command("detach", unit=name, drain=drain).result()

    def signals(self) -> dict:
        """A copy of the admission signals: answered by the dispatcher, from any thread."""
        d = self.dispatcher
        if d.on_thread() or d.thread is None or not d.thread.is_alive():
            return d.signals()
        return self.inspect("signals").result()

    # ------------------------------------------------------------ stop
    def stop(self, drain: bool = True, timeout: float = 60.0) -> None:
        """Stop admitting; finish (``drain``) or cancel resident jobs; stop plugins and units.

        The dispatcher gets ``timeout`` seconds to drain (``STOP_CANCEL_S`` at most to
        cancel), then at most ``STOP_CANCEL_S`` more to cancel; one still running then is
        marked dead (every caller answered with ``DispatcherDied``) before plugins, the
        bus and units stop.
        """
        if self._stopped:
            return
        d = self.dispatcher
        thread = d.thread
        if thread is not None and thread.is_alive():
            d.inbox.put(("stop", drain, Reply()))
            thread.join(timeout if drain else min(timeout, STOP_CANCEL_S))
            if thread.is_alive():
                d.inbox.put(("stop", False, Reply()))
                thread.join(min(timeout, STOP_CANCEL_S))
            if thread.is_alive():
                d.abandon(TimeoutError("the dispatcher did not stop in time"))

        self.plugins.notify("on_machine_stop")
        self.bus.emit("machine.stop")
        self.plugins.unload_all()
        self.bus.close()
        if self.units_started:
            stop_all([u for lane in self.lanes for u in lane.units])
        self._stopped = True

    def __enter__(self) -> "RunStage":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def _read(d: Any, source: Any, limit: int | None, buf: queue.Queue, run: RunState) -> None:
    """Source reader: up to ``limit`` items into the buffer (blocking while it is full); wakes
    the dispatcher when it waits for input, and after the last item."""
    n = 0
    try:
        items = iter(source)
        while (limit is None or n < limit) and not d.dead and not d.stopping:
            try:
                item = next(items)
            except StopIteration:
                break
            if not isinstance(item, SourceItem):
                item = SourceItem(str(next(_keys)), dict(item), auto_key=True)
            buf.put(item)
            n += 1
            if d.source_want:
                d.source_want = False
                d.inbox.put(("wake",))
    except BaseException as exc:  # noqa: BLE001
        run.source_error = exc
    finally:
        run.source_finished = True
        d.inbox.put(("wake",))


def _write(d: Any, sink: Any, sink_q: queue.Queue, run: RunState) -> None:
    """Sink writer: every record to ``sink.write`` up to the end marker; wakes the dispatcher
    when it holds records back. After a sink error the rest are taken and dropped."""
    while True:
        rec = sink_q.get()
        if d.sink_wait:
            d.sink_wait = False
            d.inbox.put(("wake",))
        if rec is SINK_END:
            return

        if run.sink_error is not None:
            continue
        try:
            sink.write(rec)
        except BaseException as exc:  # noqa: BLE001
            run.sink_error = exc
