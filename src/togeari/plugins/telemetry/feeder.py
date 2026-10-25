"""The machine side of telemetry: what the writer cannot read from the ring, as messages
(``writer.py``).

- ``initial`` (the machine stopped, at load): the machine record, the programs, the name
  table;
- ``poll`` (the telemetry thread): programs whose regions or entries changed, the name table
  when it changed, and every ``metrics_s`` one sample: the units' ``metrics()`` (read here,
  thread-safe by contract) and the machine's numbers (``PluginContext.sample``), taken by one
  call posted to the dispatcher and handed back for the next poll.

The dispatcher's share is that one posted call; its run time is the series
``telemetry/sample_us``, and the poll's own the series ``telemetry/feed_ms``.
"""

import time
from typing import Any

from togeari.plugins.telemetry.static import machine_record, program_record

# A sample asked of the dispatcher and not answered in this many sampling intervals is given
# up (the dispatcher stopped before running it)
SAMPLE_GIVE_UP = 10


def sample_points(s: dict) -> list:
    """``PluginContext.sample()`` as ``[name, unit, value]`` points."""
    points: list = []

    def put(name: str, value: Any, unit: str = "") -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            points.append([name, unit, float(value)])

    put("jobs/resident", s["resident"], "jobs")
    for state, n in s["states"].items():
        put(f"jobs/{state}", n, "jobs")
    d = s["dispatcher"]
    for k in ("cycles", "busy_s", "calls", "squashed", "wasted", "machine_errors"):
        put(f"dispatcher/{k}", d.get(k), "s" if k.endswith("_s") else "")
    for phase, sec in d.get("phase_s", {}).items():
        put(f"dispatcher/phase/{phase}", sec, "s")
    for lane in s["lanes"]:
        name = lane["name"]
        for k in ("queue", "inflight", "units_up", "waiting"):
            put(f"lane/{name}/{k}", lane["signals"].get(k))
        for k in ("submitted", "retries", "failovers"):
            put(f"lane/{name}/{k}", lane["stats"].get(k))

        # A unit's `held` is the lane's count of items sent to it and not answered;
        # `inflight` stays the engine's own number from its metrics()
        for u in lane["units"]:
            for k in ("held", "free", "busy_s", "done", "running", "waiting"):
                put(f"unit/{u['name']}/{k}", u[k], "s" if k == "busy_s" else "")
    put("telemetry/sample_us", s.get("us"), "us")
    return points


def metric_points(metrics: dict[str, dict]) -> list:
    """Each unit's ``metrics()`` as points (a value, or a ``(value, unit)`` pair)."""
    points = []
    for unit, values in metrics.items():
        for k, v in values.items():
            u = ""
            if isinstance(v, tuple):
                v, u = v
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                points.append([f"unit/{unit}/{k}", str(u), float(v)])
    return points


class Feeder:
    def __init__(self, ctx: Any, ring: Any, metrics_s: float):
        self.ctx = ctx
        self.ring = ring
        self.metrics_s = metrics_s
        self.programs: dict[int, int] = {}
        self.entries = -1
        self.names: list = []
        self.taken: dict | None = None
        self.asked = 0.0
        self.next_metrics = 0.0
        self.feed_ms = None

    def initial(self) -> list:
        """The machine record, programs and names (dispatcher thread, or the machine stopped)."""
        return [{"static": "machine", "value": machine_record(self.ctx)}, *self.statics()]

    def statics(self) -> list:
        return [*self.program_msgs(), *self.name_msgs()]

    def poll(self, final: bool = False) -> list:
        t0 = time.perf_counter()
        out = self.statics()
        now = time.monotonic()
        if now >= self.next_metrics or final:
            self.next_metrics = now + self.metrics_s
            taken, self.taken = self.taken, None
            if final and not self.ctx.machine_running():
                taken = self.timed_sample()
            if taken is not None:
                out.append({"t": taken["t"], "points": sample_points(taken)})
            points = metric_points(self.ctx.unit_metrics())
            if self.feed_ms is not None:
                points.append(["telemetry/feed_ms", "ms", self.feed_ms])
            out.append({"t": time.time(), "points": points})
            if not final:
                self.ask()
        self.feed_ms = (time.perf_counter() - t0) * 1e3
        return out

    # ------------------------------------------------------------ static records
    def program_msgs(self) -> list:
        """A record per region, again when its entries change (a region can be committed
        before its entries are registered)."""
        ctx = self.ctx
        regions = ctx.regions()
        known = list(ctx.code.entries.items())
        if len(regions) == len(self.programs) and len(known) == self.entries:
            return []
        self.entries = len(known)
        entries: dict[int, list[str]] = {}
        for name, e in known:
            entries.setdefault(e.region.start, []).append(name)
        out = []
        for r in regions:
            names = entries.get(r.start, [])
            if self.programs.get(r.start) == len(names):
                continue
            self.programs[r.start] = len(names)
            rec = program_record(r, ctx.code.mem, names)
            out.append({"static": f"program/{r.start:012d}", "value": rec})
        return out

    def name_msgs(self) -> list:
        rows = [list(r) for r in self.ring.name_rows()]
        if rows == self.names:
            return []
        self.names = rows
        return [{"static": "names", "value": rows}]

    # ------------------------------------------------------------ the dispatcher's sample
    def timed_sample(self) -> dict:
        t0 = time.perf_counter()
        s = self.ctx.sample()
        s["us"] = (time.perf_counter() - t0) * 1e6
        return s

    def ask(self) -> None:
        """Post one sample to the dispatcher, once at a time."""
        now = time.monotonic()
        if self.asked and now - self.asked < SAMPLE_GIVE_UP * self.metrics_s:
            return

        def take() -> None:
            try:
                self.taken = self.timed_sample()
            finally:
                self.asked = 0.0

        self.asked = now
        if not self.ctx.post(take):
            self.asked = 0.0
