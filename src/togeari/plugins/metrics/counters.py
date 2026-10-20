"""Per-interval counters of the dispatcher, lanes, units, jobs and work (spec/08 §8.2).

A snapshot per ``interval_s``: event counts (the bus's counts by kind),
per-node / per-lane / per-unit counts from a listener of the ``LISTENED``
kinds, and rates of the machine's totals since the previous snapshot (read on
the dispatcher thread through the plugin context). Resident jobs are counted
by state (``executing``, ``queued``, ``waiting``, ``parked``) at each sample;
``job_seconds`` is each count times the interval: pool-wide job-seconds per
state, sampled once per interval, not timed per job. Each snapshot goes to the
``watchers``, answers the ``counters`` query and, with ``emit``, is a
``counters`` bus event.
"""

import threading
import time
from collections.abc import Callable

from togeari.core.contracts.io import JobRecord
from togeari.core.plugin.base import BasePlugin, PluginContext

# Event kinds the listener counts by node, lane or unit.
LISTENED = ("stmt.fetch", "lane.enqueue", "lane.submit", "lane.result", "job.admit")


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(p * len(s)))]


def bucket_us(bucket: int) -> float:
    """The upper bound in microseconds of a cycle-time bucket (``bit_length`` of the
    microseconds).
    """
    return float(1 << bucket) if bucket > 0 else 1.0


def hist_percentile(hist: dict, p: float) -> float | None:
    """The upper bound of the bucket holding the ``p`` quantile of a bucket -> count histogram."""
    n = sum(hist.values())
    if n <= 0:
        return None
    rank = p * n
    seen = 0
    for bucket in sorted(hist):
        seen += hist[bucket]
        if seen >= rank:
            return bucket_us(bucket)
    return bucket_us(max(hist))


def _delta(now: dict, before: dict) -> dict:
    return {k: v - before.get(k, 0) for k, v in now.items() if v - before.get(k, 0)}


class Counters(BasePlugin):
    """Counters every ``interval_s``; the latest snapshot answers the ``counters`` query."""

    name = "counters"
    priority = 10

    def __init__(self, interval_s: float = 1.0, emit: bool = True, name: str = "counters"):
        self.name = name
        self.interval_s = interval_s
        self.emit = emit
        self.watchers: list[Callable[[dict], None]] = []
        self.latest: dict = {}
        self.ctx: PluginContext | None = None
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._pending = threading.Event()
        self._ticker: threading.Thread | None = None
        self._reset()

    def _reset(self) -> None:
        self._prev: dict | None = None
        self._nodes: dict[str, int] = {}
        self._enqueued: dict[str, int] = {}
        self._submits: dict[str, int] = {}
        self._queued: dict[str, list[float]] = {}
        self._service: dict[str, list[float]] = {}
        self._jobs = self._job_window()

    @staticmethod
    def _job_window() -> dict:
        return {
            "ended": 0,
            "ok": 0,
            "failed": 0,
            "seconds_sum": 0.0,
            "seconds_max": 0.0,
            "window_peak": 0,
            "strands_peak": 0,
            "squashed": 0,
            "mutates_copies": 0,
            "parks": 0,
        }

    # ------------------------------------------------------------ lifecycle
    def on_load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._reset()
        self._prev = self._totals(loads=False)
        ctx.listen(self._listen, LISTENED)

        # The ticker posts one sampling call at a time to the dispatcher.
        self._halt.clear()
        self._pending.clear()
        self._ticker = threading.Thread(
            target=self._tick_loop, name=f"togeari-{self.name}", daemon=True
        )
        self._ticker.start()

    def on_unload(self) -> None:
        self._halt.set()
        if self._ticker is not None:
            self._ticker.join(timeout=5)
            self._ticker = None
        if self.ctx is not None:
            self.ctx.unlisten(self._listen)

    def on_machine_stop(self) -> None:
        # The dispatcher has stopped: the last sample is taken here.
        self._sample()

    def contribute_queries(self) -> dict:
        return {"counters": lambda m: dict(self.latest)}

    def runtime_services(self) -> dict:
        return {self.name: self}

    # ------------------------------------------------------------ counting
    def _listen(self, kind: str, fields: dict) -> None:
        with self._lock:
            match kind:
                case "stmt.fetch":
                    node = fields.get("node")
                    self._nodes[node] = self._nodes.get(node, 0) + 1
                case "lane.enqueue":
                    lane = fields.get("lane")
                    self._enqueued[lane] = self._enqueued.get(lane, 0) + 1
                case "lane.submit":
                    unit = fields.get("unit")
                    self._submits[unit] = self._submits.get(unit, 0) + 1
                case "lane.result":
                    unit = fields.get("unit")
                    if fields.get("queued_s") is not None:
                        self._queued.setdefault(unit, []).append(fields["queued_s"])
                    if fields.get("service_s") is not None:
                        self._service.setdefault(unit, []).append(fields["service_s"])

    def on_job_end(self, record: JobRecord) -> None:
        s = record.stats
        w = self._jobs
        w["ended"] += 1
        w["ok" if record.ok else "failed"] += 1
        sec = float(s.get("seconds", 0.0))
        w["seconds_sum"] += sec
        w["seconds_max"] = max(w["seconds_max"], sec)
        w["window_peak"] = max(w["window_peak"], s.get("window_peak", 0))
        w["strands_peak"] = max(w["strands_peak"], s.get("strands_peak", 0))
        for k in ("squashed", "mutates_copies", "parks"):
            w[k] += s.get(k, 0)

    # ------------------------------------------------------------ sampling
    def _tick_loop(self) -> None:
        while not self._halt.wait(self.interval_s):
            if self._pending.is_set() or self.ctx is None:
                continue
            self._pending.set()
            if not self.ctx.post(self._tick):
                self._pending.clear()

    def _tick(self) -> None:
        self._pending.clear()
        self._sample()

    def _totals(self, loads: bool) -> dict:
        """Cumulative numbers read from the machine; dispatcher thread or stopped dispatcher."""
        ctx = self.ctx

        # Dispatcher busy time is on the perf_counter clock, unit busy time on the monotonic one
        t = time.perf_counter()
        tm = time.monotonic()
        s = ctx.stats()
        lanes = ctx.lanes(loads=loads, now=tm)
        return {
            "t": t,
            "tm": tm,
            "busy_s": s["busy_s"],
            "cycles": s["cycles"],
            "calls": s["calls"],
            "squashed": s["squashed"],
            "wasted": s["wasted"],
            "store_hits": s["store_hits"],
            "store_misses": s["store_misses"],
            "phase_s": s["phase_s"],
            "cycle_hist": s.get("cycle_hist", {}),
            "cycle_max_s": s.get("cycle_max_s", 0.0),
            "inline_s": s["inline_s"],
            "hook_s": ctx.hook_seconds(),
            "events": ctx.event_counts(),
            "lanes": {lane["name"]: lane for lane in lanes},
            "units": {u["name"]: u for lane in lanes for u in lane["units"]},
        }

    def _sample(self) -> dict:
        if self.ctx is None or self._prev is None:
            return {}
        prev, cur = self._prev, self._totals(loads=True)
        dt = max(1e-9, cur["t"] - prev["t"])
        kinds = _delta(cur["events"], prev["events"])

        # Listener counts since the previous sample
        with self._lock:
            nodes, self._nodes = self._nodes, {}
            enqueued, self._enqueued = self._enqueued, {}
            submits, self._submits = self._submits, {}
            queued, self._queued = self._queued, {}
            service, self._service = self._service, {}
        jobs, self._jobs = self._jobs, self._job_window()

        snap = {
            "t": time.time(),
            "dt": dt,
            "dispatcher": self._dispatcher(prev, cur, dt, kinds, self.ctx.events_dropped()),
            "lanes": self._lanes(prev, cur, enqueued, queued),
            "units": self._units(prev, cur, dt, cur["tm"] - prev["tm"], submits, service),
            "jobs": self._job_counters(self.ctx.job_states(), dt, jobs, kinds),
            "work": {
                "calls_per_s": (cur["calls"] - prev["calls"]) / dt,
                "calls_by_node": nodes,
                "store_hits": cur["store_hits"] - prev["store_hits"],
                "store_misses": cur["store_misses"] - prev["store_misses"],
                "wasted": cur["wasted"] - prev["wasted"],
                "squashed": cur["squashed"] - prev["squashed"],
            },
        }
        self._prev = cur
        self.latest = snap

        # Hand the snapshot on
        for fn in list(self.watchers):
            fn(snap)
        if self.emit:
            self.ctx.emit("counters", source=self.name, **snap)
        return snap

    @staticmethod
    def _dispatcher(prev: dict, cur: dict, dt: float, kinds: dict, dropped: dict) -> dict:
        inline = {}
        for node, times in cur["inline_s"].items():
            if times:
                inline[node] = {
                    "p50_us": percentile(times, 0.5) * 1e6,
                    "p99_us": percentile(times, 0.99) * 1e6,
                    "n": len(times),
                }
        return {
            "busy": (cur["busy_s"] - prev["busy_s"]) / dt,
            "cycles_per_s": (cur["cycles"] - prev["cycles"]) / dt,
            "events_per_s": sum(kinds.values()) / dt,
            "events": kinds,
            "phase_s_per_s": {
                k: v / dt for k, v in _delta(cur["phase_s"], prev["phase_s"]).items()
            },
            "inline": inline,
            "cycle": Counters._cycle(prev, cur),
            "hook_s_per_s": {k: v / dt for k, v in _delta(cur["hook_s"], prev["hook_s"]).items()},
            "dropped": dropped,
        }

    @staticmethod
    def _cycle(prev: dict, cur: dict) -> dict:
        """Cycle time (jitter) since the previous sample, from the dispatcher's histogram."""
        hist = _delta(cur["cycle_hist"], prev["cycle_hist"])
        return {
            "n": sum(hist.values()),
            "hist": hist,
            "p50_us": hist_percentile(hist, 0.5),
            "p99_us": hist_percentile(hist, 0.99),
            "max_us": bucket_us(max(hist)) if hist else None,
            "run_max_us": cur["cycle_max_s"] * 1e6,
        }

    @staticmethod
    def _lanes(prev: dict, cur: dict, enqueued: dict, queued: dict) -> dict:
        out = {}
        for name, now in cur["lanes"].items():
            # Queue waits of the items answered since the previous sample
            waits = [w for u in now["units"] for w in queued.get(u["name"], ())]

            before = prev["lanes"].get(name, {})
            stats, was = now["stats"], before.get("stats", {})
            hist = _delta(now["batch_sizes"], before.get("batch_sizes", {}))
            p50, p90 = percentile(waits, 0.5), percentile(waits, 0.9)
            out[name] = {
                **now["signals"],
                "enqueued": enqueued.get(name, 0),
                "submitted": stats["submitted"] - was.get("submitted", 0),
                "wait_ms_p50": None if p50 is None else p50 * 1e3,
                "wait_ms_p90": None if p90 is None else p90 * 1e3,
                "batch_sizes": hist,
                "retries": stats["retries"] - was.get("retries", 0),
                "failovers": stats["failovers"] - was.get("failovers", 0),
                "no_unit_s": stats["no_unit_s"],
            }
        return out

    @staticmethod
    def _units(prev: dict, cur: dict, dt: float, dtm: float, submits: dict, service: dict) -> dict:
        """Per unit; ``dtm`` is the interval on the monotonic clock unit busy time uses (0 when
        the clock has not ticked, and then the busy time has not grown either)."""
        out = {}
        for name, now in cur["units"].items():
            before = prev["units"].get(name, {})
            times = service.get(name, [])
            p50, p90 = percentile(times, 0.5), percentile(times, 0.9)
            busy = now["busy_s"] - before.get("busy_s", 0.0)
            out[name] = {
                "lane": now["lane"],
                "state": now["state"],
                "inflight": now["inflight"],
                "busy": busy / dtm if dtm > 0 else 0.0,
                "rate_ewma": now["rate"],
                "items_per_s": (now["done"] - before.get("done", 0)) / dt,
                "submits": submits.get(name, 0),
                "submit_ms": now["submit_s"] * 1e3,
                "service_ms_p50": None if p50 is None else p50 * 1e3,
                "service_ms_p90": None if p90 is None else p90 * 1e3,
                "load": now.get("load", {}),
                "errors": now["errors"],
            }
        return out

    @staticmethod
    def _job_counters(states: dict, dt: float, jobs: dict, kinds: dict) -> dict:
        ended = jobs.pop("ended")
        seconds = jobs.pop("seconds_sum")
        return {
            "resident": sum(states.values()),
            **states,
            # Job-seconds per state over the interval, from the states at the sample
            "job_seconds": {state: n * dt for state, n in states.items()},
            "admitted": kinds.get("job.admit", 0),
            "ended": ended,
            "seconds_mean": seconds / ended if ended else None,
            **jobs,
        }
