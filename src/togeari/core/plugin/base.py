"""Plugins: behaviour at the seams between the machine's parts (spec/10).

Hook kinds:

- transform (``pre_*`` / ``post_*``): return None to keep the value, or a new
  value that replaces it for the next plugin and the machine;
- block: a ``pre_*`` hook raises ``PluginBlockError``;
- veto (``vote_*``): return False to stop the action;
- callback (``on_*``): fire-and-forget;
- contribution (``contribute_*``, ``runtime_services``): called at load;
  ``contribute_policies`` before the programs are bound.

A hook that raises (other than a block) or returns a value of the wrong type
is a plugin error: logged, its change skipped, and the plugin disabled when
the ``plugin_errors`` knob is "disable". Hot-path hooks run on the dispatcher
thread. ``on_event`` runs on the plugin's own thread, fed through a bounded
queue.
"""

import copy
import threading
import time
from typing import Any

from togeari.core.runtime.state import InfState, StrandState

# What a resident job is doing, as counted by ``PluginContext.job_states``
JOB_STATES = ("executing", "queued", "waiting", "parked")


class PluginBlockError(Exception):
    """Raised by a ``pre_*`` hook to stop the operation."""


def job_state(job: Any) -> str:
    """One resident job's state: ``executing`` (a call handed to a unit), ``queued`` (calls
    issued, none handed to a unit yet), ``waiting`` (statements waiting on values or a gate,
    or strands stalled on the window), ``parked`` (paused, or strands parked at a branch
    with nothing else unfinished).

    Cost: a job with issued calls is scanned in window order up to its first call handed to
    a unit, or up to its last issued call; any other job costs a few attribute reads."""
    if job.paused:
        return "parked"

    # Issued calls: handed to a unit, or still in a lane queue; the scan ends at the last one
    if job.issued:
        seen = 0
        for inf in job.window:
            if inf.state != InfState.ISSUED:
                continue
            if inf.item is not None and "sent" in inf.item.ext:
                return "executing"
            seen += 1
            if seen == job.issued:
                break
        return "queued"

    # Nothing issued: statements or strands waiting, else parked
    if job.unfinished or job.runnable:
        return "waiting"
    if any(st.state == StrandState.STALLED for st in job.strands.values()):
        return "waiting"
    return "parked"


def _unit_view(lane: Any, st: Any, now: float, loads: bool) -> dict:
    busy = st.busy_total + (now - st.busy_since if st.busy_since is not None else 0.0)
    view = {
        "name": st.unit.name,
        "lane": lane.name,
        "state": st.state,
        "inflight": st.inflight,
        "rate": st.rate,
        "done": st.items_done,
        "busy_s": busy,
        "submit_s": st.submit_lat,
        "errors": dict(st.errors),
    }
    if loads:
        try:
            ld = st.unit.load()
            view["load"] = {"running": ld.running, "waiting": ld.waiting, **ld.extra}
        except Exception:  # noqa: BLE001
            view["load"] = {}
    return view


def _lane_view(lane: Any, now: float, loads: bool) -> dict:
    return {
        "name": lane.name,
        "kind": lane.kind,
        "config": {
            "former": lane.former,
            "select": lane.select,
            "forming": lane.forming,
            "max_inflight": lane.max_inflight,
            "retry": lane.retry,
            "timeout_s": lane.timeout_s,
            "lane_dead_s": lane.lane_dead_s,
        },
        "signals": lane.signals(),
        "stats": dict(lane.stats),
        "batch_sizes": dict(lane.batch_sizes),
        "units": [_unit_view(lane, st, now, loads) for st in lane.state.values()],
    }


class PluginContext:
    """What a plugin may reach: signals, code memory, queries, commands, events, knobs,
    and read-only views of the machine.

    Reads marked "dispatcher thread" are answered on the dispatcher thread (a hook,
    ``post``, a query) or while the machine is stopped; every value they return
    is a copy the plugin owns.
    """

    def __init__(self, machine: Any, plugin: "BasePlugin"):
        self._m = machine
        self.plugin = plugin

    @property
    def code(self):
        return self._m.code

    @property
    def knobs(self) -> dict:
        return dict(self._m.knobs)

    @property
    def libraries(self) -> list:
        """The machine's node libraries, as given."""
        return list(self._m.libraries)

    @property
    def sig_path(self) -> list | None:
        """The machine's signature search path, or None."""
        path = self._m.sig_path
        return None if path is None else list(path)

    def machine_running(self) -> bool:
        """True while the dispatcher thread runs; from any thread."""
        d = getattr(self._m, "dispatcher", None)
        return d is not None and d.thread is not None and d.thread.is_alive()

    def for_plugin(self, plugin: "BasePlugin") -> "PluginContext":
        """A context of the same machine for a plugin this plugin runs inside itself."""
        return PluginContext(self._m, plugin)

    def query_names(self) -> list[str]:
        return sorted(self._m.queries)

    def command_names(self) -> list[str]:
        return sorted(self._m.commands)

    # ------------------------------------------------------------ machine views
    def stats(self) -> dict:
        """A deep copy of the dispatcher's counters (dispatcher thread)."""
        return copy.deepcopy(self._m.dispatcher.stats)

    def summary(self, seconds: float) -> dict:
        """The machine's run totals over ``seconds`` (dispatcher thread)."""
        return self._m.summary(seconds)

    def unit_specs(self) -> dict[str, Any]:
        """Each lane unit's ``spec()``, by unit name; ``{"error": repr}`` when it raises."""
        specs: dict[str, Any] = {}
        for lane in self._m.lanes:
            for unit in lane.units:
                try:
                    specs[unit.name] = unit.spec()
                except Exception as exc:  # noqa: BLE001
                    specs[unit.name] = {"error": repr(exc)}
        return specs

    def resident_jobs(self) -> int:
        """Jobs resident in the pool (dispatcher thread)."""
        return len(self._m.dispatcher.jobs)

    def job_states(self) -> dict[str, int]:
        """Resident jobs by ``JOB_STATES`` (dispatcher thread); one ``job_state`` per resident
        job."""
        counts = dict.fromkeys(JOB_STATES, 0)
        for job in list(self._m.dispatcher.jobs.values()):
            counts[job_state(job)] += 1
        return counts

    def hook_seconds(self) -> dict[str, float]:
        """Seconds spent in each plugin's hooks so far (dispatcher thread)."""
        return {name: sum(t.values()) for name, t in self._m.plugins.timing.items()}

    def event_counts(self) -> dict[str, int]:
        """Events emitted so far, by kind (dispatcher thread)."""
        return dict(self._m.bus.counts)

    def events_dropped(self) -> dict[str, int]:
        """Events each subscribed plugin's queue dropped so far."""
        return self._m.bus.dropped()

    def construction(self) -> dict:
        """How the machine was built: ``default_entry``, ``passes``, ``bind`` (pattern ->
        binding), ``store``, ``contributed_policies`` (names) and ``warnings``. The
        bindings and the store are the machine's objects, to describe, not to change.
        """
        m = self._m
        return {
            "default_entry": m.default_entry,
            "passes": m.passes,
            "bind": dict(m.bind),
            "store": m.store,
            "contributed_policies": sorted(m.contributed_policies),
            "warnings": list(m.warnings),
        }

    def lanes(self, loads: bool = False, now: float | None = None) -> list[dict]:
        """Every lane as a dict (dispatcher thread): ``name``, ``kind``, ``config`` (its
        former, selector, forming, max in flight, retry, timeout and dead time; the
        policies are the lane's objects, to describe), ``signals``, ``stats``,
        ``batch_sizes`` and ``units``; see ``units``. Running busy spans are measured
        up to ``now`` (``time.monotonic()`` when None).
        """
        now = time.monotonic() if now is None else now
        return [_lane_view(lane, now, loads) for lane in self._m.lanes]

    def units(self, loads: bool = False) -> list[dict]:
        """Every unit as a dict (dispatcher thread): ``name``, ``lane``, ``state``,
        ``inflight``, ``rate``, ``done``, ``busy_s`` (busy time including a running busy
        span), ``submit_s`` (submit latency EWMA), ``errors``; with ``loads``, ``load``:
        the unit's ``load()`` as a dict (empty when it raises).
        """
        return [u for lane in self.lanes(loads) for u in lane["units"]]

    # ------------------------------------------------------------ bus listeners
    def listen(self, fn: Any, kinds: Any = None) -> None:
        """Call ``fn(kind, fields)`` on the dispatcher thread for each event of ``kinds``
        (None: every kind); ``fn`` must be cheap and must not keep ``fields``' objects.
        """
        self._m.bus.listen(fn, kinds)

    def unlisten(self, fn: Any) -> None:
        self._m.bus.unlisten(fn)

    def signals(self) -> dict:
        """A copy of the admission signals, from any thread."""
        return self._m.signals()

    def inspect(self, query: str, /, **args: Any):
        return self._m.inspect(query, **args)

    def command(self, cmd: str, /, **args: Any):
        return self._m.command(cmd, **args)

    def post(self, fn: Any) -> bool:
        """Run ``fn()`` on the dispatcher thread between cycles; False when it is not running.

        An error in ``fn`` is this plugin's error.
        """
        if not self.machine_running():
            return False
        self._m.dispatcher.inbox.put(("call", fn, self.plugin.name))
        return True

    def query_now(self, query: str, /, **args: Any) -> Any:
        """Answer a query directly: on the dispatcher thread, or when the machine is stopped."""
        return self._m.queries[query](self._m, **args)

    def emit(self, kind: str, **fields: Any) -> None:
        """Emit a bus event. From another thread while the machine runs, the emit is posted
        and runs on the dispatcher thread between cycles, so event counts and listeners only
        ever run there; an emit posted as the machine stops may not run."""
        d = getattr(self._m, "dispatcher", None)
        if self.machine_running() and threading.current_thread() is not d.thread:
            bus = self._m.bus
            d.inbox.put(("call", lambda: bus.emit(kind, **fields), self.plugin.name))
            return
        self._m.bus.emit(kind, **fields)

    def service(self, name: str) -> Any:
        return self._m.services.get(name)


class BasePlugin:
    """Every hook is optional; the default does nothing."""

    name: str = "plugin"
    # Lower runs earlier.
    priority: int = 50
    # Static gate, e.g. {"entries": [...], "lanes": [...]}; None applies everywhere.
    applies_to: dict | None = None
    # Bus subscription for on_event: "all", "sampled", "counters" or None (no events).
    events: str | None = None
    # When the plugin's event queue is full: "drop", "sample" or "counters_only".
    overflow: str = "drop"
    queue_size: int = 10_000

    def should_apply(self, ctx: PluginContext) -> bool:
        """Dynamic gate, evaluated before each hook call."""
        return True

    # Lifecycle (callbacks).
    def on_load(self, ctx: PluginContext) -> None: ...

    def on_unload(self) -> None: ...

    def on_machine_start(self) -> None: ...

    def on_machine_stop(self) -> None: ...
