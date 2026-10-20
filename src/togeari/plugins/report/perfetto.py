"""The ``perfetto`` plugin: a Chrome trace JSON file (opens in Perfetto and chrome://tracing).

Tracks:

- process ``dispatcher``: a counter track per dispatcher phase, plus busy fraction;
- process ``lanes``: a counter track per lane queue (queued, in flight);
- process ``units``: a thread per unit with a slice per answered item (its
  service time), submit and state instants, and a counter track per unit
  (busy, items per second);
- process ``jobs``: an async track per job from admission to end, with its
  statement and fault instants.

The file is a JSON array written as events arrive; the closing bracket is
written at unload, and trace viewers also load the file without it. The
writer holds at most ``queue_size`` tasks; tasks past it are dropped and
counted (``perfetto`` query). At stop the plugin unloads by one deadline, ``close_s``
from the start of its unload, shared by the delivery of its queued events and the writer's
drain; then the file closes and tasks still queued write nothing.
"""

import functools
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from togeari.core.plugin.base import BasePlugin, PluginContext
from togeari.plugins.common.machine import json_default
from togeari.plugins.common.worker import Worker, left
from togeari.plugins.metrics.counters import Counters

PIDS = {"dispatcher": 1, "lanes": 2, "units": 3, "jobs": 4}

log = logging.getLogger("togeari.plugins")


class Perfetto(BasePlugin):
    """Writes a trace of the run to ``path``; counter tracks are sampled every ``sample_s``."""

    name = "perfetto"
    priority = 810
    events = "all"
    queue_size = 100_000

    def __init__(self, path: str | Path, sample_s: float = 0.25, close_s: float = 10.0):
        self.path = Path(path)
        self.sample_s = sample_s
        self.close_s = close_s
        self.counters: Counters | None = None
        self.worker: Worker | None = None
        self.t0 = 0.0
        self.tids: dict[str, int] = {}
        self.written = 0
        self.dropped = 0

        # Every file write holds this lock; once closed, or from an earlier load, a task
        # writes nothing
        self._io = threading.Lock()
        self._closed = False
        self._gen = 0

    # ------------------------------------------------------------ lifecycle
    def on_load(self, ctx: PluginContext) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.t0 = time.time()
        self.tids.clear()

        # A new generation: a writer of an earlier load writes nothing into this file
        with self._io:
            self._gen += 1
            self._closed = False
            self._f = self.path.open("w", encoding="utf-8")
            self._f.write("[\n")
            self._first = True
        gen = self._gen
        self.worker = Worker(
            f"togeari-{self.name}",
            functools.partial(self._handle, gen),
            functools.partial(self._flush, gen),
            maxsize=self.queue_size,
        )

        # Process names
        for name, pid in PIDS.items():
            self.worker.put(
                ("raw", {"ph": "M", "name": "process_name", "pid": pid, "args": {"name": name}})
            )

        # Counter tracks come from a private sampler
        self.counters = Counters(interval_s=self.sample_s, emit=False, name="perfetto.counters")
        self.counters.on_load(ctx.for_plugin(self.counters))
        self.counters.watchers.append(self._on_counters)

    def on_machine_stop(self) -> None:
        if self.counters is not None:
            self.counters.on_machine_stop()

    def on_unload(self) -> None:
        deadline = self.unload_by()
        if self.counters is not None:
            self.counters.on_unload()
            self.counters = None
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.close(timeout=left(deadline, self.close_s))
            self.dropped = worker.dropped

        # The array closes; a writer still running skips its remaining tasks
        with self._io:
            self._closed = True
            self._f.write("\n]\n")
            self._f.close()
        if worker is not None and worker.thread.is_alive():
            log.warning(
                "perfetto writer did not finish in %ss; queued events dropped", self.close_s
            )
            worker.close(timeout=left(deadline, 0.0))

    def contribute_queries(self) -> dict:
        return {"perfetto": lambda m: self.status()}

    def status(self) -> dict:
        dropped = self.worker.dropped if self.worker is not None else self.dropped
        return {"path": str(self.path), "written": self.written, "dropped": dropped}

    # ------------------------------------------------------------ inputs
    def on_event(self, ev: Any) -> None:
        if self.worker is not None:
            self.worker.put(("event", ev))

    def _on_counters(self, snap: dict) -> None:
        if self.worker is not None:
            self.worker.put(("counters", snap))

    # ------------------------------------------------------------ trace events
    def _ts(self, t: float) -> float:
        return round((t - self.t0) * 1e6, 3)

    def _write(self, ev: dict) -> None:
        sep = "" if self._first else ",\n"
        self._first = False
        self._f.write(sep + json.dumps(ev, default=json_default))
        self.written += 1

    def _flush(self, gen: int) -> None:
        with self._io:
            if gen == self._gen and not self._closed:
                self._f.flush()

    def _unit_tid(self, unit: str) -> int:
        tid = self.tids.get(unit)
        if tid is None:
            tid = self.tids[unit] = len(self.tids) + 1
            self._write(
                {
                    "ph": "M",
                    "name": "thread_name",
                    "pid": PIDS["units"],
                    "tid": tid,
                    "args": {"name": unit},
                }
            )
        return tid

    def _counter(self, process: str, name: str, ts: float, args: dict) -> None:
        self._write(
            {"ph": "C", "name": name, "pid": PIDS[process], "tid": 0, "ts": ts, "args": args}
        )

    def _handle(self, gen: int, task: tuple) -> None:
        with self._io:
            if gen == self._gen and not self._closed:
                self._write_task(task)

    def _write_task(self, task: tuple) -> None:
        match task:
            case ("raw", ev):
                self._write(ev)
            case ("counters", snap):
                self._counters(snap)
            case ("event", ev):
                self._event(ev)

    def _counters(self, snap: dict) -> None:
        ts = self._ts(snap["t"])
        d = snap["dispatcher"]

        # Dispatcher phases and busy fraction
        self._counter("dispatcher", "busy", ts, {"fraction": d["busy"]})
        for phase, v in d["phase_s_per_s"].items():
            self._counter("dispatcher", f"phase {phase}", ts, {"ms_per_s": v * 1e3})

        # Lane queues
        for lane, ln in snap["lanes"].items():
            self._counter(
                "lanes", f"queue {lane}", ts, {"queued": ln["queue"], "inflight": ln["inflight"]}
            )

        # Units
        for unit, u in snap["units"].items():
            self._counter(
                "units", f"unit {unit}", ts, {"busy": u["busy"], "items_per_s": u["items_per_s"]}
            )

    def _event(self, ev: Any) -> None:
        ts = self._ts(ev.t)
        f = ev.fields
        kind = ev.kind

        # One trace record per event family
        match kind.split(".")[0]:
            # Job begin / end as an async slice
            case "job":
                ph = "b" if kind == "job.admit" else "e"
                self._write(
                    {
                        "ph": ph,
                        "cat": "job",
                        "name": f"job {f.get('key')}",
                        "id": f.get("job"),
                        "pid": PIDS["jobs"],
                        "tid": 0,
                        "ts": ts,
                        "args": {k: v for k, v in f.items() if k != "stats"},
                    }
                )

            # Statement and fault events as instants on the job's slice
            case "stmt" | "fault" if f.get("job") is not None:
                self._write(
                    {
                        "ph": "n",
                        "cat": "job",
                        "name": kind if kind != "fault.taken" else f"fault {f.get('error_kind')}",
                        "id": f["job"],
                        "pid": PIDS["jobs"],
                        "tid": 0,
                        "ts": ts,
                        "args": f,
                    }
                )

            # A unit's service time as a complete slice on its track
            case "lane" if kind == "lane.result" and f.get("service_s") is not None:
                self._write(
                    {
                        "ph": "X",
                        "name": f.get("node") or kind,
                        "pid": PIDS["units"],
                        "tid": self._unit_tid(f.get("unit") or "?"),
                        "ts": self._ts(ev.t - f["service_s"]),
                        "dur": round(f["service_s"] * 1e6, 3),
                        "args": f,
                    }
                )

            # Other lane and unit events as instants on the unit's track
            case "lane" | "unit" if f.get("unit") is not None:
                self._write(
                    {
                        "ph": "i",
                        "s": "t",
                        "name": kind,
                        "pid": PIDS["units"],
                        "tid": self._unit_tid(f["unit"]),
                        "ts": ts,
                        "args": f,
                    }
                )

            # Machine events as global instants
            case "machine":
                self._write(
                    {
                        "ph": "i",
                        "s": "g",
                        "name": kind,
                        "pid": PIDS["dispatcher"],
                        "tid": 0,
                        "ts": ts,
                    }
                )
