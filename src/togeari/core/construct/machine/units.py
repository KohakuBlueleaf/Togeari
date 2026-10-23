"""Units while running: start off the dispatcher thread, attach, detach, lane wiring and events."""

import functools
import threading
import time
import weakref
from concurrent.futures import Future
from typing import Any

from togeari.core.construct.errors import ConstructionError, Problem
from togeari.core.construct.units import start_all, stop_all
from togeari.core.lanes.lane import Lane
from togeari.core.lanes.native import adopt
from togeari.core.program.bind import check_unit
from togeari.core.program.execution import note_execution_change


class UnitStage:
    """Units joining and leaving lanes (mixed into ``Machine``)."""

    # ------------------------------------------------------------ starting
    def _start_off_thread(self, units: list, then: Any, fut: Future) -> None:
        """Start units on a helper thread, then run ``then(failures)`` on the dispatcher thread;
        a ``then`` that raises fails ``fut``."""
        lanes = dict(self._lane_of_unit)
        d = self.dispatcher

        def finish(failures: list) -> None:
            try:
                then(failures)
            except Exception as exc:  # noqa: BLE001
                d.event_error("start", exc)
                if not fut.done():
                    fut.set_exception(exc)

        def go() -> None:
            failures = start_units_of(units, self.start_timeout_s, lanes)
            d.inbox.put(("call", lambda: finish(failures), None))

        threading.Thread(target=go, name="togeari-start", daemon=True).start()

    # ------------------------------------------------------------ attach and detach
    def attach_now(self, lane_name: str, unit: Any) -> Future:
        """Check, start (off the dispatcher thread), then add a unit to a lane (`attach`)."""
        lane = next((ln for ln in self.lanes if ln.name == lane_name), None)
        if lane is None:
            raise KeyError(f"no lane {lane_name!r}")
        problems = self._attach_problems(lane, unit)
        if problems:
            raise ConstructionError([Problem(f"lane {lane.name!r}", p) for p in problems])
        fut: Future = Future()

        def started(failures: list) -> None:
            if failures:
                stop_all([unit])
                fut.set_exception(ConstructionError(failures))
                return
            lane.attach(unit)
            self._lane_of_unit[id(unit)] = lane
            fut.set_result(unit.name)

        if self.units_started:
            self._start_off_thread([unit], started, fut)
        else:
            started([])
        return fut

    def _attach_problems(self, lane: Lane, unit: Any) -> list[str]:
        out = []
        if any(unit is u for ln in self.lanes for u in ln.units):
            out.append(f"unit {unit.name!r} is already attached")
        elif any(u.name == unit.name for ln in self.lanes for u in ln.units):
            out.append(f"unit name {unit.name!r} is used twice")
        kind = getattr(unit, "kind", "call")
        if lane.units and kind != lane.kind:
            out.append(f"unit {unit.name!r} is a {kind} unit; the lane is {lane.kind}")

        # Every node bound to this lane must run on the new unit
        for ins in self.code.mem:
            b = ins.binding
            if b is None or b.inline or b.lane is not lane:
                continue
            out += check_unit(ins, b.adapter, unit, self.defaults.protocol)
        return list(dict.fromkeys(out))

    def detach_now(self, unit_name: str, drain: bool) -> int:
        for lane in self.lanes:
            for u in lane.units:
                if u.name == unit_name:
                    return len(lane.detach(u, drain, time.monotonic()))
        raise KeyError(f"no unit {unit_name!r}")

    def _stop_detached(self, name: str) -> None:
        """Stop (off the dispatcher thread) a unit that left its lane, unless a lane holds it."""
        held = {id(u) for ln in self.lanes for u in ln.units}
        for ln in self.lanes:
            for st in ln.state.values():
                u = st.unit
                if u.name == name and id(u) not in held:
                    threading.Thread(target=stop_all, args=([u],), daemon=True).start()
                    return

    # ------------------------------------------------------------ lanes
    def identity(self, adapter: Any, lane: Lane) -> tuple:
        """What a content key holds of a binding: the adapter's identity and the lane's engines'."""
        return (adapter_identity(adapter), lane.engine_identity())

    def _wire(self, lane: Lane) -> None:
        d = self.dispatcher
        lane.on_done = d.lane_done
        lane.on_wake = d.lane_woke
        lane.hook_submit = d.before_submit
        lane.emit = functools.partial(self.lane_event, lane.name)
        lane.wants = self.bus.wants

        # Observe only semantic lane changes; callbacks do not keep a stopped machine alive.
        machine = weakref.ref(self)

        def changed(lane, kind):
            owner = machine()
            if owner is not None:
                note_execution_change(owner, lane, kind)

        lane._execution_notify = changed
        lane._watch_retry()

        # The native core runs its lanes' queues and issue logic too
        if self.core == "rust":
            adopt(lane)

    def lane_event(self, lane_name: str, kind: str, **fields: Any) -> None:
        """Lane events (with the lane's name) go to the bus; unit state changes also to
        `on_unit_state` plugins."""
        fields.setdefault("lane", lane_name)
        if kind in ("unit.attach", "unit.detach", "unit.detached"):
            lane = next((ln for ln in self.lanes if ln.name == lane_name), None)
            if lane is not None:
                note_execution_change(self, lane, "unit_membership")
        self.bus.emit(kind, **fields)
        if kind == "unit.detached" and self.units_started:
            self._stop_detached(fields.get("unit"))
        if kind.startswith("unit."):
            self.plugins.notify(
                "on_unit_state", fields.get("unit"), kind[5:], where={"lane": fields.get("lane")}
            )


def start_units_of(units: list, timeout: float, lanes: dict) -> list[Problem]:
    """Start units; every failure as a construction problem under its lane."""
    out = []
    for u, msg in start_all(units, timeout):
        lane = lanes.get(id(u))
        where = f"lane {lane.name!r}" if lane is not None else "units"
        out.append(Problem(where, f"unit {u.name!r}: {msg}"))
    return out


def adapter_identity(adapter: Any) -> str:
    spec = getattr(adapter, "spec", None)
    if callable(spec):
        try:
            return repr(spec())
        except Exception:  # noqa: BLE001
            pass
    fn = getattr(adapter, "fn", None)
    name = getattr(fn, "__qualname__", None) if fn is not None else None
    return f"{type(adapter).__module__}.{type(adapter).__qualname__}:{name}"
