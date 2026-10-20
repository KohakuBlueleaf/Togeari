"""The ``run_report`` plugin: one directory that answers every question about a run (spec/08 §8.3).

```
<dir>/
  spec.json          programs, hashes, entries, passes, bindings, lanes, unit specs, policies,
                     plugins, knobs
  env.json           interpreter, OS, package versions, unit specs
  metrics.jsonl      one counters snapshot per interval
  events.jsonl[.zst] every bus event the plugin receives (zstd when zstandard is installed)
  programs/<hash>.json  programs of the recorded jobs, one file per semantic hash
  runs/<run>/        run.json and the conformance records of the recorded jobs (record.kv,
                     or record.pkl)
  summary.json       totals, throughput, slowest lanes, top waits, plugin hook time, weak
                     digests and weak values, records written and tasks dropped by full
                     writer queues
```

Everything is written as the run goes. Events and metrics go through one writer
thread whose queue holds at most ``queue_size`` tasks; tasks past it are dropped
and counted. The summary does not queue: each counters snapshot replaces one
pending summary, which the writer writes before its next task or when idle, so
an event flood never drops it. ``summary.json`` is written as a temporary file,
then replaced (retried while a reader holds it open), by one writer at a time. At
stop the plugin unloads by one deadline, ``close_s`` from the start of its unload,
shared by the delivery of its queued events, the recorder's drain and the writer's
drain; then the files close and the last summary is written.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from togeari.core.contracts.io import JobRecord
from togeari.core.plugin.base import BasePlugin, PluginContext
from togeari.plugins.common.machine import dumps, plain
from togeari.plugins.common.worker import Worker, left
from togeari.plugins.metrics.counters import Counters
from togeari.plugins.record.recorder import Recorder
from togeari.plugins.report.env import environment
from togeari.plugins.report.spec import machine_spec

try:
    import zstandard
except ImportError:
    zstandard = None

log = logging.getLogger("togeari.plugins")

# Attempts and pause of a replace that a reader's open handle refuses
REPLACE_TRIES = 5
REPLACE_PAUSE_S = 0.05


class RunReport(BasePlugin):
    """Writes the run report into ``path``; ``compress`` None means zstd when available."""

    name = "run_report"
    priority = 800
    events = "all"
    queue_size = 100_000

    def __init__(
        self,
        path: str | Path,
        compress: bool | None = None,
        interval_s: float = 1.0,
        flush_s: float = 1.0,
        close_s: float = 10.0,
    ):
        self.path = Path(path)
        self.compress = (zstandard is not None) if compress is None else compress
        if self.compress and zstandard is None:
            raise ValueError("compress=True needs zstandard")
        self.interval_s = interval_s
        self.flush_s = flush_s
        self.close_s = close_s
        self.recorder = Recorder(self.path, queue_size=self.queue_size, close_s=close_s)

        # The pending summary (latest only) and the lock every file write holds
        self._pending: dict | None = None
        self._io = threading.Lock()
        self._closed = False
        self.ctx: PluginContext | None = None
        self.counters: Counters | None = None
        self.own_counters = False
        self.worker: Worker | None = None
        self.started: float | None = None
        self.stopped: float | None = None
        self.last: dict = {}
        self.metric_lines = 0
        self.event_lines = 0

    # ------------------------------------------------------------ lifecycle
    def on_load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.path.mkdir(parents=True, exist_ok=True)
        self.started = time.perf_counter() if ctx.machine_running() else None
        self.stopped = None
        self._pending = None
        self._closed = False

        # Static descriptions of the machine and its environment
        units = {name: plain(spec) for name, spec in ctx.unit_specs().items()}
        self._write_json("spec.json", machine_spec(ctx))
        self._write_json("env.json", {**environment(), "units": units})

        # Open files; the worker owns them from here on
        self._metrics = (self.path / "metrics.jsonl").open("a", encoding="utf-8")
        if self.compress:
            raw = (self.path / "events.jsonl.zst").open("ab")
            self._events_raw = raw
            self._events = zstandard.ZstdCompressor(level=3).stream_writer(raw)
        else:
            self._events_raw = None
            self._events = (self.path / "events.jsonl").open("a", encoding="utf-8")
        self._last_flush = time.monotonic()
        self.worker = Worker(
            f"togeari-{self.name}", self._handle, self._flush_maybe, maxsize=self.queue_size
        )

        # Counters: the machine's shared ones, or a private instance
        shared = ctx.service("counters")
        if isinstance(shared, Counters):
            self.counters, self.own_counters = shared, False
        else:
            self.counters = Counters(
                interval_s=self.interval_s, emit=False, name="run_report.counters"
            )
            self.counters.on_load(ctx.for_plugin(self.counters))
            self.own_counters = True
        self.counters.watchers.append(self._on_counters)
        self.recorder.on_load(ctx)

    def on_unload(self) -> None:
        deadline = self.unload_by()
        if self.counters is not None:
            if self._on_counters in self.counters.watchers:
                self.counters.watchers.remove(self._on_counters)
            if self.own_counters:
                self.counters.on_unload()
            self.counters = None

        # Drain the recorder and the writer, both by the unload's one deadline
        archive = self.recorder.writer
        self.recorder.unload_deadline = deadline
        try:
            self.recorder.on_unload()
        finally:
            self.recorder.unload_deadline = None
        records = self._records(archive)
        worker, self.worker = self.worker, None
        overran = False
        if worker is not None:
            worker.close(timeout=left(deadline, self.close_s))
            records["writer_dropped"] = worker.dropped
            overran = worker.thread.is_alive()
            if overran:
                log.warning(
                    "run_report writer did not finish in %ss; %d tasks left unwritten",
                    self.close_s,
                    worker.q.qsize(),
                )

        # From here the writer writes nothing; the files close and the last summary is written
        with self._io:
            self._closed = True
            self._pending = None
            self._metrics.close()
            self._events.close()
            if self._events_raw is not None and not self._events_raw.closed:
                self._events_raw.close()
            self._write_json("summary.json", {**self.summary(self._seconds()), **records})

        # An overrun writer skips its remaining tasks, then ends
        if overran:
            worker.close(timeout=left(deadline, 0.0))

    def contribute_queries(self) -> dict:
        return {"run_report": lambda m: self.status()}

    def status(self) -> dict:
        return {
            "path": str(self.path),
            "event_lines": self.event_lines,
            "metric_lines": self.metric_lines,
            "writer_dropped": self.worker.dropped if self.worker is not None else 0,
            "records": self.recorder.status(),
        }

    def on_machine_start(self) -> None:
        if self.started is None:
            self.started = time.perf_counter()

    def on_machine_stop(self) -> None:
        self.stopped = time.perf_counter()
        if self.own_counters and self.counters is not None:
            self.counters.on_machine_stop()

    # ------------------------------------------------------------ recording
    def on_job_start(self, job: dict) -> None:
        self.recorder.on_job_start(job)

    def on_job_end(self, record: JobRecord) -> None:
        if self.own_counters and self.counters is not None:
            self.counters.on_job_end(record)
        self.recorder.on_job_end(record)

    # ------------------------------------------------------------ streams
    def on_event(self, ev: Any) -> None:
        if self.worker is not None:
            self.worker.put(("event", ev))

    def _on_counters(self, snap: dict) -> None:
        self.last = snap
        worker = self.worker
        if worker is None:
            return
        worker.put(("metrics", snap))

        # The summary so far, read here (dispatcher thread); it replaces the pending one
        records = {**self._records(self.recorder.writer), "writer_dropped": worker.dropped}
        self._pending = {**self.summary(self._seconds()), **records}

    def _handle(self, task: tuple) -> None:
        with self._io:
            if self._closed:
                return
            self._write_pending()
            match task:
                case ("event", ev):
                    line = dumps(ev.to_dict()) + "\n"
                    self._events.write(line.encode() if self.compress else line)
                    self.event_lines += 1
                case ("metrics", snap):
                    self._metrics.write(dumps(snap) + "\n")
                    self._metrics.flush()
                    self.metric_lines += 1

    def _flush_maybe(self) -> None:
        with self._io:
            if self._closed:
                return
            self._write_pending()
            now = time.monotonic()
            if now - self._last_flush < self.flush_s:
                return
            self._last_flush = now
            if self.compress:
                self._events.flush(zstandard.FLUSH_BLOCK)
                self._events_raw.flush()
            else:
                self._events.flush()

    def _write_pending(self) -> None:
        """Write the pending summary, if any (writer thread, ``_io`` held)."""
        data, self._pending = self._pending, None
        if data is not None:
            self._write_json("summary.json", data)

    # ------------------------------------------------------------ summary
    def _seconds(self) -> float:
        """Run time so far: from machine start to stop, or to now while running."""
        if self.started is None:
            return 0.0
        end = self.stopped if self.stopped is not None else time.perf_counter()
        return end - self.started

    @staticmethod
    def _records(archive: Any) -> dict:
        """Records written and dropped, and the written jobs with weak values."""
        if archive is None:
            return {"records": 0, "records_dropped": 0, "weak_jobs": 0, "weak_values": 0}
        return {
            "records": archive.written,
            "records_dropped": archive.dropped,
            "weak_jobs": archive.weak_jobs,
            "weak_values": archive.weak_values,
        }

    def summary(self, seconds: float) -> dict:
        """Totals, throughput, slowest lanes, top waits, weak digests; dispatcher thread or
        stopped dispatcher."""
        ctx = self.ctx
        totals = ctx.summary(seconds)
        stats = ctx.stats()
        weak = stats.get("weak_digests", {})

        # Lanes by their p90 wait, and each unit's busy share
        lanes = (self.last or {}).get("lanes", {})
        waits = sorted(
            ((name, ln.get("wait_ms_p90")) for name, ln in lanes.items()),
            key=lambda x: -(x[1] or 0.0),
        )
        busy = {u["name"]: u["busy_s"] / seconds if seconds > 0 else 0.0 for u in ctx.units()}

        return {
            **totals,
            "slowest_lanes": [name for name, _ in waits],
            "top_waits_ms_p90": dict(waits[:10]),
            "unit_busy": busy,
            "weak_digests": weak,
            "weak_digest_count": sum(weak.values()),
            "store_weak": stats.get("store_weak", 0),
            "plugins": ctx.query_now("plugins"),
            "events": ctx.event_counts(),
            "dropped": ctx.events_dropped(),
            "metric_lines": self.metric_lines,
            "event_lines": self.event_lines,
        }

    def _write_json(self, name: str, data: Any) -> None:
        tmp = self.path / (name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=repr), encoding="utf-8")

        # A reader holding the file open refuses the replace on Windows: retried, then warned
        for attempt in range(REPLACE_TRIES):
            try:
                tmp.replace(self.path / name)
                return
            except PermissionError:
                if attempt + 1 < REPLACE_TRIES:
                    time.sleep(REPLACE_PAUSE_S)
        log.warning("run_report could not replace %s; it stays at its previous version", name)
