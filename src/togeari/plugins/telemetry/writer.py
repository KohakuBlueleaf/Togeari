"""The telemetry writer: drains the ring and writes the run directory; it knows no machine.

Each ``step``, in one pass: apply the messages the machine side sent (static records, series
points); drain the ring; append the raw records; fold them into the open 1 s buckets; close
the buckets that are complete (one second behind the wall clock) and merge them into the 1 min
and 1 h tiers; keep traces; every ``metrics_s`` its own series (the ring's and the writer's
counts). The time each step took is itself a series (``writer/tick_ms``). It runs on a thread
of the machine's process or in a process of its own (``child.py``).

Messages from the machine side (``feeder.py``), plain JSON values:

- ``{"static": key, "value": v}``: a ``static.kv`` record (``static.py``);
- ``{"t": wall, "points": [[name, unit, value], ...]}``: one sample of named series.
"""

import logging
import math
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
from kohakuvault import KVault

from togeari.plugins.telemetry import layout as L
from togeari.plugins.telemetry.hist import sparse
from togeari.plugins.telemetry.rollup import Rows, Tier, observe
from togeari.plugins.telemetry.store import Segments
from togeari.plugins.telemetry.traces import Traces

log = logging.getLogger("togeari.telemetry")

# Retention runs at most this often; tick times kept for ``status``; writes held while the
# files refuse them (one tick makes a handful)
RETAIN_EVERY_S = 60.0
TICKS_KEPT = 3600
MAX_PENDING = 1 << 16
STOP_TRIES = 3

# The knobs the writer reads (``Telemetry``'s of the same names)
KNOBS = (
    "flush_s",
    "max_drain",
    "store",
    "segment_s",
    "raw_keep_s",
    "raw_keep_bytes",
    "r1s_keep_s",
    "r1m_keep_s",
    "trace_rate",
    "trace_window_s",
    "trace_max_records",
    "metrics_s",
)


class Writer:
    def __init__(self, cfg: Any, ring: Any, root: Path):
        self.p = cfg
        self.ring = ring
        self.root = root
        self.epoch = ring.epoch_wall

        # Files
        self.raw = Segments(root, "raw", L.RAW_COLUMNS, cfg.segment_s)
        self.tiers = [
            (Tier(period), Segments(root, name, {**L.ROLLUP_COLUMNS, "h": L.hist_dtype(name)}, seg))
            for name, period, seg in L.TIERS
        ]
        self.metrics = Segments(root, "metrics", L.METRIC_COLUMNS, L.METRICS_SEGMENT_S)
        self.static = KVault(str(root / L.STATIC_FILE))
        self.events = KVault(str(root / L.EVENTS_FILE))
        self.traces = Traces(cfg.trace_window_s, cfg.trace_max_records, cfg.trace_rate)

        # Series ids, the next own sample and retention, writes the files refused
        self.series: dict[str, int] = {}
        self.next_metrics = 0.0
        self.next_retain = 0.0
        self.pending: deque = deque()

        # The writer's own counts, and the time of each of the last steps
        self.ticks_ms: deque = deque(maxlen=TICKS_KEPT)
        self.stats = {
            "ticks": 0,
            "records": 0,
            "tick_ms": 0.0,
            "tick_ms_max": 0.0,
            "errors": 0,
            "dropped_writes": 0,
            "deleted_files": 0,
            "traces": 0,
            "messages": 0,
        }

    # ------------------------------------------------------------ one step
    def step(self, messages: list, final: bool = False) -> None:
        t0 = time.perf_counter()
        try:
            for msg in messages:
                self.apply(msg)
            n = self._tick(final)
        except Exception:
            self.stats["errors"] += 1
            log.warning("telemetry: writer step failed", exc_info=True)
            return
        ms = (time.perf_counter() - t0) * 1e3
        self.ticks_ms.append(ms)
        s = self.stats
        s["ticks"] += 1
        s["records"] += n
        s["tick_ms"] = ms
        s["tick_ms_max"] = max(s["tick_ms_max"], ms)

    def apply(self, msg: dict) -> None:
        self.stats["messages"] += 1
        match msg:
            case {"static": str(key), "value": value}:
                self.static[key] = value
            case {"t": t, "points": points}:
                self.put_points(t, [(self.sid(n, u), v) for n, u, v in points])
            case _:
                raise ValueError(f"not a writer message: {sorted(msg)}")

    def _tick(self, final: bool) -> int:
        data = self.ring.drain(self.p.max_drain)
        recs = np.frombuffer(data, dtype=L.REC)
        if not self.p.store:
            return len(recs)

        # In memory first (raw records, the rollup tiers, traces), as writes in order
        if len(recs):
            sec = np.floor(self.epoch + recs["t_ns"] / 1e9).astype(np.int64)
            raw = {"rec": recs.view(np.uint8).reshape(len(recs), L.REC.itemsize)}
            self.pending.append((self.raw.append, (sec, raw)))
        self.tiers[0][0].add(observe(recs, self.epoch))
        cutoff = math.inf if final else math.floor(time.time()) - 1
        carry: Rows | None = None
        for tier, seg in self.tiers:
            if carry is not None:
                tier.add(carry)
            carry = tier.close(cutoff)
            if len(carry):
                self.pending.append((self._write_rows, (seg, carry)))
        for k, v in self.traces.add(recs, self.epoch):
            self.pending.append((self.events.__setitem__, (k, v)))
            self.stats["traces"] += 1

        self.flush()

        # Own series, retention
        now = time.monotonic()
        if now >= self.next_metrics or final:
            self.next_metrics = now + self.p.metrics_s
            self.sample()
        if now >= self.next_retain:
            self.next_retain = now + RETAIN_EVERY_S
            self.retain()
        return len(recs)

    def flush(self) -> None:
        """The pending writes, in order: a write that raises stays pending (with every later
        one) for the next tick; past ``MAX_PENDING`` the oldest are dropped and counted."""
        while len(self.pending) > MAX_PENDING:
            self.pending.popleft()
            self.stats["dropped_writes"] += 1
        while self.pending:
            fn, args = self.pending[0]
            fn(*args)
            self.pending.popleft()

    def _write_rows(self, seg: Segments, rows: Rows) -> None:
        h = rows.h
        hist = [sparse(r) for r in h] if seg.name == "r1s" else np.minimum(h, 0xFFFFFFFF)
        seg.append(
            rows.t,
            {
                "t": rows.t,
                "ent": rows.ent,
                "met": rows.met,
                "n": rows.n,
                "sum": rows.sum,
                "lo": rows.lo,
                "hi": rows.hi,
                "h": hist if isinstance(hist, list) else hist.astype(np.uint32),
            },
        )

    def close(self) -> None:
        """Writes still refused are tried ``STOP_TRIES`` times more, then the files close."""
        for _ in range(STOP_TRIES):
            if not self.pending:
                break
            try:
                self.flush()
            except Exception:
                self.stats["errors"] += 1
                log.warning("telemetry: a write failed at stop", exc_info=True)
        self.stats["dropped_writes"] += len(self.pending)
        self.pending.clear()
        for k, v in self.traces.flush():
            self.events[k] = v
        self.raw.close()
        for _, seg in self.tiers:
            seg.close()
        self.metrics.close()
        self.static.close()
        self.events.close()

    # ------------------------------------------------------------ series
    def sid(self, name: str, unit: str = "") -> int:
        got = self.series.get(name)
        if got is None:
            got = self.series[name] = len(self.series) + 1
            self.static[f"series/{got:08d}"] = {"name": name, "unit": unit}
        return got

    def put_points(self, t: float, points: list[tuple[int, float]]) -> None:
        if not points:
            return
        sids, values = zip(*points)
        self.metrics.append(
            np.full(len(points), int(t), np.int64),
            {
                "t": np.full(len(points), t),
                "sid": np.array(sids, np.uint32),
                "v": np.array(values, np.float64),
            },
        )

    def sample(self) -> None:
        """The ring's and the writer's own counts."""
        own = ("records", "tick_ms", "tick_ms_max", "errors", "dropped_writes", "traces")
        points = [
            *((self.sid(f"ring/{k}", "records"), float(v)) for k, v in self.ring.counts().items()),
            *((self.sid(f"writer/{k}", "ms" if "tick" in k else ""), self.stats[k]) for k in own),
            (self.sid("writer/pending", "writes"), len(self.pending)),
        ]
        self.put_points(time.time(), points)

    # ------------------------------------------------------------ retention
    def retain(self) -> None:
        p = self.p
        now = time.time()
        deleted = self.raw.retain(now, p.raw_keep_s, p.raw_keep_bytes)
        for (_, seg), keep in zip(self.tiers, (p.r1s_keep_s, p.r1m_keep_s, None)):
            deleted += seg.retain(now, keep, None)
        deleted += self.metrics.retain(now, p.r1m_keep_s, None)
        self.stats["deleted_files"] += deleted
