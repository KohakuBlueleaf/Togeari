"""The ``telemetry`` plugin: the machine's records, rollups, traces and series on disk.

It attaches a telemetry ring to the machine (``togeari.core.observe.ring``; the native one on
the native core) and runs the writer (``writer.py``) into ``dir`` (layout: ``layout.py``),
fed by ``feeder.py``. A machine without this plugin has no ring and pays one branch per write
site.

Knobs:

- ``writer``: "process" (a process of its own reading the ring through a file mapping, so
  the writer never holds the machine's GIL), "thread" (a thread of the machine's process),
  "auto" (process unless the ring is the Python one);
- ``ring``: "auto" (native on the native core or with a process writer, else Python),
  "native", "python"; ``ring_records``: its capacity (a full ring drops records and counts
  them); ``ring_file``: the mapping's file (None: ``/dev/shm`` when present, else the
  temporary directory);
- ``store``: False drains the ring and writes nothing (to measure the ring alone);
- ``flush_s``: the writer's tick; ``max_drain``: records per tick at most;
- ``segment_s``: seconds per raw file; ``raw_keep_s`` / ``raw_keep_bytes``: raw retention;
  ``r1s_keep_s``, ``r1m_keep_s``: rollup retention (1 h rows are kept);
- ``trace_rate``, ``trace_window_s``, ``trace_max_records``: job traces (``traces.py``);
- ``metrics_s``: the series sampling interval;
- ``inspector_token``: write the inspector's access token into ``machine.json`` (for
  ``togeari dash``; anyone who can read the directory can then drive the machine).
"""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from togeari.core.contracts.telemetry import DEFAULT_RECORDS
from togeari.core.plugin.base import BasePlugin, PluginContext, native_ring
from togeari.plugins.telemetry import layout as L
from togeari.plugins.telemetry.feeder import Feeder
from togeari.plugins.telemetry.runner import Child, Local

RINGS = ("auto", "native", "python")
WRITERS = ("auto", "process", "thread")
SHM = "/dev/shm"


def ring_file() -> Path:
    """A new file name for a shared ring: in ``/dev/shm`` when present, else the temp dir."""
    base = SHM if os.path.isdir(SHM) and os.access(SHM, os.W_OK) else tempfile.gettempdir()
    return Path(base) / f"togeari-{os.getpid()}-{uuid.uuid4().hex[:12]}.ring"


class Telemetry(BasePlugin):
    name = "telemetry"
    priority = 5

    def __init__(
        self,
        dir: str | os.PathLike,
        writer: str = "auto",
        ring: str = "auto",
        ring_records: int = DEFAULT_RECORDS,
        ring_file: str | os.PathLike | None = None,
        store: bool = True,
        flush_s: float = 1.0,
        max_drain: int = 1 << 22,
        segment_s: int = 600,
        raw_keep_s: float | None = 6 * 3600,
        raw_keep_bytes: int | None = 8 << 30,
        r1s_keep_s: float | None = 3 * 86400,
        r1m_keep_s: float | None = 30 * 86400,
        trace_rate: float = 0.001,
        trace_window_s: float = 600.0,
        trace_max_records: int = 4096,
        metrics_s: float = 1.0,
        inspector_token: bool = False,
        name: str = "telemetry",
    ):
        if ring not in RINGS:
            raise ValueError(f"ring must be one of {RINGS}, not {ring!r}")
        if writer not in WRITERS:
            raise ValueError(f"writer must be one of {WRITERS}, not {writer!r}")
        if writer == "process" and ring == "python":
            raise ValueError("a process writer reads a native ring, not the Python one")
        self.name = name
        self.dir = Path(dir)
        self.writer_kind = writer
        self.ring_kind = ring
        self.ring_records = ring_records
        self.ring_file = ring_file
        self.store = store
        self.flush_s = flush_s
        self.max_drain = max_drain
        self.segment_s = segment_s
        self.raw_keep_s = raw_keep_s
        self.raw_keep_bytes = raw_keep_bytes
        self.r1s_keep_s = r1s_keep_s
        self.r1m_keep_s = r1m_keep_s
        self.trace_rate = trace_rate
        self.trace_window_s = trace_window_s
        self.trace_max_records = trace_max_records
        self.metrics_s = metrics_s
        self.inspector_token = inspector_token
        self.ctx: PluginContext | None = None
        self.ring: Any = None
        self.runner: Local | Child | None = None
        self.started = 0.0

    def process_writer(self) -> bool:
        """True when the writer runs in a process of its own (``writer`` resolved)."""
        match self.writer_kind:
            case "process":
                return True
            case "thread":
                return False
            case _:
                return native_ring() and self.ring_kind != "python"

    # ------------------------------------------------------------ lifecycle
    def on_load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.dir.mkdir(parents=True, exist_ok=True)
        process = self.process_writer()
        path = (Path(self.ring_file) if self.ring_file else ring_file()) if process else None
        self.ring = ctx.make_ring(self.ring_kind, self.ring_records, path)
        self.started = time.time()

        # Every lane and unit known now has its id before any record names it
        for lane in ctx.lanes():
            self.ring.name_id("lane", lane["name"])
            for u in lane["units"]:
                self.ring.name_id("unit", u["name"])
        self.write_machine()

        # Attach first (a ring the core refuses raises before any thread, process or file
        # opens); a writer that fails to start detaches the ring again
        ctx.set_telemetry(self.ring)
        feeder = Feeder(ctx, self.ring, self.metrics_s)
        try:
            runner = (Child if process else Local)(self, feeder, self.ring, self.dir)
            runner.start(feeder.initial())
            self.runner = runner
        except BaseException:
            ctx.set_telemetry(None)
            raise

    def on_machine_start(self) -> None:
        # The inspector (a later plugin) is known now
        self.write_machine()

    def on_unit_state(self, unit: Any, state: str) -> None:
        if self.ring is None or not isinstance(unit, str):
            return
        match state:
            case "attach":
                self.ring.name_id("unit", unit)
            case "detached":
                self.ring.leave("unit", unit)

    def on_unload(self) -> None:
        if self.ctx is not None and self.ring is not None:
            self.ctx.set_telemetry(None)
        if self.runner is not None:
            self.runner.stop()
        self.write_machine(stopped=time.time())

    def runtime_services(self) -> dict:
        return {self.name: self}

    def contribute_queries(self) -> dict:
        return {"telemetry": lambda m: self.status()}

    def status(self) -> dict:
        """The ring's counts and the writer's (they stay readable after the machine stops)."""
        r = self.runner
        return {
            "dir": str(self.dir),
            "ring": None if self.ring is None else dict(self.ring.counts()),
            "writer": None if r is None else r.status(),
        }

    # ------------------------------------------------------------ machine.json
    def write_machine(self, stopped: float | None = None) -> None:
        ctx = self.ctx
        insp = ctx.service("inspector") if ctx is not None else None
        r = self.runner
        info = {
            "version": 1,
            "name": self.dir.name,
            "started": self.started,
            "stopped": stopped,
            "epoch_wall": None if self.ring is None else self.ring.epoch_wall,
            "core": ctx.construction()["core"] if ctx is not None else None,
            "ring": None if self.ring is None else type(self.ring).__name__,
            "writer": "process" if isinstance(r, Child) else "thread" if r is not None else None,
            "pid": os.getpid(),
            "inspector": None if insp is None else insp.url,
            "inspector_token": insp.token if insp is not None and self.inspector_token else None,
            "tiers": {name: {"period": p, "file_s": s} for name, p, s in L.TIERS},
            "segment_s": self.segment_s,
        }
        tmp = self.dir / f"{L.MACHINE_FILE}.tmp"
        tmp.write_text(json.dumps(info, indent=1), encoding="utf-8")
        os.replace(tmp, self.dir / L.MACHINE_FILE)
