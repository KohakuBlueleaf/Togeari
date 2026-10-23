"""Unit health of a lane: unit states, health polls, retries with backoff, item timeouts,
the dead-lane limit, and elastic units (spec/04 §4.5).

``ItemError`` never changes a unit's state; ``UnitError`` takes the unit out of service
until ``ready()`` reports true again.
"""

import contextlib
import heapq
from dataclasses import dataclass, field
from typing import Any

from togeari.core.contracts.results import Cancelled, ItemError, UnitUnavailable
from togeari.core.lanes.deadline import current, expire
from togeari.core.lanes.queue import FRONT, Queue

# Health-poll backoff of a down unit: first wait and ceiling, in seconds.
POLL_FIRST_S = 0.5
POLL_MAX_S = 30.0


class State:
    """Unit states within a lane."""

    UP = "up"
    DOWN = "down"
    DRAINING = "draining"
    DETACHED = "detached"


@dataclass
class Retry:
    """Re-queues per item after ``UnitError``s; each waits ``backoff_ms`` before it is queued."""

    max: int = 3
    backoff_ms: float = 200.0


@dataclass
class UnitState:
    unit: Any
    queue: Queue
    state: str = State.UP
    inflight: int = 0
    # Open submission ids in submit order
    subs: dict = field(default_factory=dict)
    # Items per second, EWMA
    rate: float = 0.0
    busy_since: float | None = None
    busy_total: float = 0.0
    items_done: int = 0
    errors: dict = field(default_factory=dict)
    next_poll: float = 0.0
    backoff: float = POLL_FIRST_S
    submit_lat: float = 0.0


class Health:
    """Lane methods for unit states and everything driven by time (mixed into ``Lane``)."""

    # ------------------------------------------------------------ states
    def _set_state(self, st: UnitState, state: str) -> None:
        st.state = state
        self._up = None
        self.up_version += 1
        self._pump_needed = True

    def unit_down(self, unit: Any, err: Any, now: float) -> None:
        """Take ``unit`` out of service after a ``UnitError``; its queued items go to the
        shared queue."""
        st = self.state[id(unit)]
        if st.state != State.UP:
            return
        self._set_state(st, State.DOWN)

        # Next health poll: the engine's Retry-After, else the backoff
        delay = err.retry_after if getattr(err, "retry_after", None) else st.backoff
        st.next_poll = now + delay
        st.backoff = min(POLL_MAX_S, st.backoff * 2)

        # Counters, early-forming queue, event
        k = type(err.error).__name__ if hasattr(err, "error") else "UnitError"
        st.errors[k] = st.errors.get(k, 0) + 1
        self.stats["failovers"] += 1
        self._move_to_shared(st, now)
        self.emit("unit.down", unit=unit.name, reason=str(getattr(err, "error", err)))

    def _poll(self, st: UnitState, now: float) -> None:
        """Health poll of a down unit: up when ``ready()`` reports true, else again later."""
        try:
            ok = bool(st.unit.ready())
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            st.next_poll = now + st.backoff
            st.backoff = min(POLL_MAX_S, st.backoff * 2)
            return

        self._set_state(st, State.UP)
        st.backoff = POLL_FIRST_S
        self.stats["recoveries"] += 1
        self.emit("unit.up", unit=st.unit.name, reason="ready")

    def _move_to_shared(self, st: UnitState, now: float) -> None:
        for order, item in st.queue.drain():
            self._q.push(item, order, now)

    def _check_drained(self, st: UnitState, now: float) -> None:
        if st.state == State.DRAINING and st.inflight == 0:
            self._detached(st, now, "drained")

    def _detached(self, st: UnitState, now: float, reason: str) -> None:
        """``st``'s unit is detached: it leaves ``units`` and ``unit.detached`` is emitted (the
        machine stops it when no other lane holds it)."""
        self._set_state(st, State.DETACHED)
        self._leave(st, now)
        self.emit("unit.detached", unit=st.unit.name, reason=reason)

    # ------------------------------------------------------------ elastic units
    def attach(self, unit: Any) -> None:
        """Add a started ``unit``, or bring a detached or draining one back.

        The unit is up when ``ready()`` reports true, else down and polled with backoff. A
        unit already in the lane, or another unit with its name, raises ValueError; a
        detached unit with the name is forgotten.
        """
        now = self.clock()
        st = self.state.get(id(unit))
        match st:
            case None:
                if any(u.name == unit.name for u in self.units):
                    raise ValueError(f"lane {self.name!r} has a unit named {unit.name!r}")
                for k in [k for k, s in self.state.items() if s.unit.name == unit.name]:
                    del self.state[k]
                st = self._join(unit, now)
            case UnitState(state=State.DETACHED):
                st = self._join(unit, now)
            case UnitState(state=State.DRAINING):
                pass
            case _:
                raise ValueError(f"unit {unit.name!r} is already in lane {self.name!r}")
        st.backoff = POLL_FIRST_S

        # Up when ready, down and polled otherwise
        try:
            ok = bool(unit.ready())
        except Exception:  # noqa: BLE001
            ok = False
        self._set_state(st, State.UP if ok else State.DOWN)
        st.next_poll = now + st.backoff
        self.emit("unit.attach", unit=unit.name, reason="attach")

    def detach(self, unit: Any, drain: bool, now: float) -> list:
        """Stop giving ``unit`` work; returns the items re-queued.

        ``drain=True``: in-flight items finish; the unit is ``draining`` and becomes
        ``detached`` when its last item settles. ``drain=False``: its open submissions are
        cancelled and their unsettled items re-queued at the front, except submissions
        holding an ``ordered`` item, which finish on the unit (the unit drains them); with
        none left the unit is ``detached`` at once. A detached unit leaves ``units`` (its
        state stays, for views) and ``unit.detached`` is emitted.
        """
        st = self.state.get(id(unit))
        if st is None or st.state == State.DETACHED:
            return []
        self._move_to_shared(st, now)
        if drain:
            self._set_state(st, State.DRAINING)
            self.emit("unit.detach", unit=unit.name, reason="drain")
            self._check_drained(st, now)
            return []

        # Cancel every open submission and re-queue its unsettled items; a submission
        # holding an ordered item finishes on this unit
        requeued = []
        kept = False
        for sid in list(st.subs):
            sub = self.subs[sid]
            if any(it.ext.get("ordered_once") for it in sub.items):
                kept = True
                continue
            with contextlib.suppress(Exception):
                unit.cancel(sub.ticket)
            self._close(sub, now)
            for it in sub.items:
                if not it.ext.get("settled") and not it.ext.get("squashed"):
                    self.requeue_front(it, now)
                    requeued.append(it)

        # Under early forming a re-queued item may have gone to this unit's own queue
        self._move_to_shared(st, now)
        self.emit("unit.detach", unit=unit.name, reason="now")
        if kept:
            self._set_state(st, State.DRAINING)
            self._check_drained(st, now)
        else:
            self._detached(st, now, "now")
        return requeued

    # ------------------------------------------------------------ time
    def timers(self, now: float) -> list[tuple[Any, Any]]:
        """Health polls, retries past their backoff, item timeouts and the dead-lane limit;
        returns the ``(item, result)`` pairs to deliver.

        Dead lane: once no unit has been up for ``lane_dead_s``, every queued or held item
        fails with ``ItemError(UnitUnavailable)`` and ``lane.dead`` is emitted with the
        count; items queued later fail at the next call while no unit is up.
        """
        out: list[tuple[Any, Any]] = []

        # Health polls of down units
        for st in self.state.values():
            if st.state == State.DOWN and now >= st.next_poll:
                self._poll(st, now)

        # Retries whose backoff ended go to the front of the queue
        held = self._held
        while held and held[0][0] <= now:
            _, _, item = heapq.heappop(held)
            if self._held_ids.pop(id(item), None) is not None:
                self.requeue_front(item, now)

        # Item timeouts: a waiting item leaves the lane; one in flight is settled, and a
        # submission whose items all settled is cancelled
        expired = []
        tq = self._timeouts
        while tq and tq[0][0] <= now:
            _, _, item, sub = heapq.heappop(tq)
            if not self._timing(item, sub):
                continue
            item = current(item)
            # The persistent call-cap entry may fire while the item is in flight.
            active = sub or self.subs.get(item.ext.get("sub"))
            result = expire(item, now, active is not None and active.open)
            if result is None:
                continue
            item.ext["settled"] = True
            out.append((item, result))
            if active is None or not active.open:
                self.remove(item)
            else:
                expired.append(active)
        for sub in expired:
            if sub.open and all(it.ext.get("settled") for it in sub.items):
                with contextlib.suppress(Exception):
                    sub.unit.cancel(sub.ticket)
                out.extend(self.results(sub, [Cancelled()] * len(sub.items), now))

        # Dead lane: queued and held items fail
        if self._dead(now):
            failed = self._fail_queued()
            if failed:
                self.stats["dead_failed"] += len(failed)
                self.emit("lane.dead", lane=self.name, items=len(failed))
                err = UnitUnavailable(f"lane {self.name}: no unit up for {self.lane_dead_s}s")
                out.extend((it, ItemError(err)) for it in failed)
        return out

    def _dead(self, now: float) -> bool:
        # The same sum ``next_deadline`` reports, so the clock reaching it makes the lane dead
        return (
            self.lane_dead_s is not None
            and self.no_unit_since is not None
            and now >= self.no_unit_since + self.lane_dead_s
        )

    def _fail_queued(self) -> list:
        failed = [item for q in self._queues() for _, item in q.drain()]
        failed.extend(self._held_ids.values())
        self._held.clear()
        self._held_ids.clear()
        for it in failed:
            it.ext["settled"] = True
        return failed

    def _hold(self, item: Any, now: float) -> None:
        """Re-queue a retried item after the retry backoff."""
        wait = self.retry.backoff_ms / 1000.0
        if wait <= 0:
            self.requeue_front(item, now)
            return
        self._held_ids[id(item)] = item
        heapq.heappush(self._held, (now + wait, next(self._seq), item))

    def requeue_front(self, item: Any, now: float | None = None) -> None:
        """Put ``item`` back ahead of every queued item."""
        now = self.clock() if now is None else now
        self._route(item, (FRONT, -next(self._seq)), now)
