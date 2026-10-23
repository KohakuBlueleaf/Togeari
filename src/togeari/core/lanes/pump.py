"""Multi-issue of a lane: every unit with free capacity takes work each pump (spec/04 §4.4)."""

import heapq
import itertools
import logging
import time
from dataclasses import dataclass
from typing import Any

from togeari.core.contracts.resources import EstimateError
from togeari.core.contracts.results import ItemError, UnitError
from togeari.core.lanes.deadline import arm
from togeari.core.lanes.health import State
from togeari.core.lanes.resources import post_transform

log = logging.getLogger("togeari.lane")
_sub_ids = itertools.count(1)

# Queue waits kept between two ``take_waits`` calls.
_WAITS_MAX = 20_000

# Timeout-heap size over the items in flight at which the heap is rebuilt.
_TIMEOUT_SLACK = 256


@dataclass
class Submission:
    id: int
    unit: Any
    items: list
    ticket: Any = None
    sent: float = 0.0
    open: bool = True


class Pump:
    """Lane methods that hand queued items to units (mixed into ``Lane``)."""

    def pump(self, now: float) -> int:
        """Submit to every unit up with free capacity; returns the items submitted.

        Each unit takes, per ``capacity()`` slot read at the start of its turn, the items
        it admits from the first ripe bucket of its queue (the shared one under late
        forming, its own under early forming).
        """
        self._pump_needed = False
        self._pumped_at = now
        up = self.units_up()

        # No unit up: the dead-lane clock runs, and its time counts as it passes
        if not up:
            if self.no_unit_since is None:
                self.no_unit_since = self.no_unit_counted = now
            self.stats["no_unit_s"] += now - self.no_unit_counted
            self.no_unit_counted = now
            return 0
        if self.no_unit_since is not None:
            self.stats["no_unit_s"] += now - self.no_unit_counted
            self.no_unit_since = None
        if not self.queued_now():
            return 0

        # Early forming: shared-queue items go to unit queues first
        early = self.forming == "early"
        if early and len(self._q):
            self._spread(up, now)

        # Each unit in selector order, up to its capacity
        sent = 0
        order = self._order(up)
        for unit in order:
            st = self.state[id(unit)]
            queue = st.queue if early else self._q
            if not len(queue):
                continue
            queue.promote(now)
            try:
                cap = unit.capacity()
            except Exception:  # noqa: BLE001
                cap = 0

            # Early forming: items of the unit's own queue it no longer admits are noted
            refused: dict = {}
            admits = self._admits if not early else self._noting(refused)
            while cap > 0 and self.under_cap() and st.state == State.UP:
                items = queue.take(unit, self._count, admits, now)
                if not items:
                    break
                self._submit(unit, items, now)
                sent += len(items)
                cap -= 1
            if refused:
                self._respread(unit, refused, order, now)
        return sent

    def _noting(self, refused: dict) -> Any:
        """``_admits`` that notes each item it refuses in ``refused`` (by id, once)."""

        def admits(item: Any, unit: Any) -> bool:
            ok = self._admits(item, unit)
            if not ok:
                refused.setdefault(id(item), item)
            return ok

        return admits

    def _respread(self, unit: Any, refused: dict, order: list, now: float) -> None:
        """Refused items still in ``unit``'s queue move, with their order key, to the first other
        unit in ``order`` that admits them (taken this pump, or the next); an item no other unit
        admits stays."""
        queue = self.state[id(unit)].queue
        for item in refused.values():
            e = queue.index.get(id(item))
            if e is None:
                continue
            to = next((u for u in order if u is not unit and self._admits(item, u)), None)
            if to is None:
                continue
            queue.remove(item, now)
            self.state[id(to)].queue.push(item, e.order, now)
            self._pump_needed = True

    # ------------------------------------------------------------ policies, contained
    def _order(self, up: list) -> list:
        """The selector's unit order over the units up (each once); the given order when it
        raises."""
        try:
            order = list(self.select.order(up, self))
        except Exception as exc:  # noqa: BLE001
            self._policy_failed("selector order", exc)
            return list(up)
        ids = {id(u) for u in up}
        out, seen = [], set()
        for u in order:
            if id(u) in ids and id(u) not in seen:
                seen.add(id(u))
                out.append(u)
        return out

    def _admits(self, item: Any, unit: Any) -> bool:
        """The selector's admission of ``item`` to ``unit``; True when it raises."""
        try:
            return bool(self.select.admits(item, unit, self))
        except Exception as exc:  # noqa: BLE001
            self._policy_failed("selector admits", exc)
            return True

    def _count(self, bucket: Any, unit: Any, now: float) -> int | list:
        """How many items one submit to ``unit`` carries from ``bucket``, or which; the batch
        limit when the former raises."""
        if getattr(unit, "kind", "call") != "batch":
            return 1
        limit = max(1, getattr(unit, "max_batch", 1) or 1)
        if self.former is None:
            return limit
        try:
            want = self.former.take(bucket, unit, now)
        except Exception as exc:  # noqa: BLE001
            self._policy_failed("former take", exc)
            if self._resources is not None and self._resources.policy is not None:
                for item in bucket.candidates(limit):
                    self._resources.reject(
                        item, ItemError(EstimateError("resource selection failed"))
                    )
                    self.remove(item)
                return []
            return limit
        return want[:limit] if isinstance(want, list) else min(want or 0, limit)

    def _policy_failed(self, what: str, exc: BaseException) -> None:
        """A lane policy raised: counted in the lane's ``policy_errors``, logged at the 1st,
        2nd, 4th, 8th ... error."""
        n = self.stats["policy_errors"] = self.stats.get("policy_errors", 0) + 1
        if n & (n - 1) == 0:
            log.error(
                "lane %s: %s failed (%d so far); fallback used", self.name, what, n, exc_info=exc
            )

    # ------------------------------------------------------------ routing
    def _spread(self, up: list, now: float) -> None:
        """Shared-queue items to unit queues: the first unit in selector order admitting each."""
        order = self._order(up) or list(up)
        for o, item in self._q.drain():
            unit = next((u for u in order if self._admits(item, u)), order[0])
            self.state[id(unit)].queue.push(item, o, now)

    def _route(self, item: Any, order: tuple, now: float) -> None:
        """Queue ``item``: a unit queue under early forming with a unit up, else the shared one."""
        self._pump_needed = True
        up = self.units_up()
        if self.forming == "early" and up:
            cands = self._order(up) or list(up)
            unit = next((u for u in cands if self._admits(item, u)), cands[0])
            self.state[id(unit)].queue.push(item, order, now)
            return
        self._q.push(item, order, now)

    # ------------------------------------------------------------ submissions
    def _submit(self, unit: Any, items: list, now: float) -> None:
        # Expired call caps never reach a unit; timers settle them from the held set.
        live = []
        for item in items:
            cap = item.ext.get("deadline_cap")
            if cap is not None and now >= cap:
                self._held_ids[id(item)] = item
            else:
                live.append(item)
        items = live
        if not items:
            return

        original = list(items) if self._resources is not None else None
        # Plugins see (and may block) items after unit choice
        if self.hook_submit is not None:
            items = self.hook_submit(self, unit, items)
            if self._resources is not None:
                for item in original:
                    self._resources.move(item, None)
            if not items:
                return
            # User preparation consumes the call budget, not the submitted-attempt budget.
            now = max(now, self.clock())
            live = []
            for item in items:
                cap = item.ext.get("deadline_cap")
                if cap is not None and now >= cap:
                    self._held_ids[id(item)] = item
                else:
                    live.append(item)
            items = live
            if not items:
                return

        if self._resources is not None:
            items = post_transform(self, original, items, unit, now)
            if not items:
                return

        # Resource estimation, compatibility keys and repacking also consume the call budget.
        # Keep expired items held for normal timer settlement, before allocating an attempt.
        now = max(now, self.clock())
        live = []
        for item in items:
            cap = item.ext.get("deadline_cap")
            if cap is not None and now >= cap:
                self._held_ids[id(item)] = item
            else:
                live.append(item)
        items = live
        if not items:
            return

        # Submission and in-flight accounting
        sub = Submission(next(_sub_ids), unit, items, sent=now)
        st = self.state[id(unit)]
        self.subs[sub.id] = sub
        st.subs[sub.id] = None
        st.inflight += len(items)
        self.inflight += len(items)
        if st.busy_since is None:
            st.busy_since = now

        # Per item: protocol, submission, queue wait, timeout
        protocol = getattr(unit, "protocol", None)
        for it in items:
            it.protocol = protocol or it.protocol
            it.ext["sub"] = sub.id
            it.ext["unit"] = unit.name
            it.ext["sent"] = now
            self.waits.append(now - it.ext.get("enqueued", now))
            self._arm_timeout(it, sub, now)
            if self._resources is not None:
                self._resources.move(it, "inflight")
        if len(self.waits) > _WAITS_MAX:
            del self.waits[: _WAITS_MAX // 2]

        # Counters
        batch = getattr(unit, "kind", "call") == "batch"
        self.stats["submitted"] += len(items)
        if len(items) > 1 or batch:
            self.stats["batches"] += 1
            self.batch_sizes[len(items)] = self.batch_sizes.get(len(items), 0) + 1

        # Hand the work to the unit; a submit that raises answers every item with UnitError
        lane = self

        def done(ticket, results, sub=sub):
            lane.on_done(lane, sub, results)

        # One attempt per actual item submission, after transforms/expiry; submit errors count.
        for item in items:
            item._begin_attempt()
        t0 = time.perf_counter()
        try:
            sub.ticket = unit.submit(items if batch else items[0], done)
        except Exception as exc:  # noqa: BLE001
            self.on_done(self, sub, [UnitError(exc)] * len(items))
        st.submit_lat = time.perf_counter() - t0
        # The event carries the first item's job, so job sampling applies to it, and each item's
        # (job key, address, order key)
        jid = items[0].ext.get("job")
        if self.wants("lane.submit", jid):
            self.emit(
                "lane.submit",
                job=jid,
                lane=self.name,
                unit=unit.name,
                items=len(items),
                work=[_place(it) for it in items],
            )

    def _arm_timeout(self, item: Any, sub: Submission, now: float) -> None:
        """Arm the min of the original call cap and this attempt's submitted-time cap."""
        deadline = arm(item, now, self.timeout_s)
        if deadline is None:
            return
        heapq.heappush(self._timeouts, (deadline, next(self._seq), item, sub))

        # Entries of settled items are dropped once they outnumber the items in flight
        if len(self._timeouts) > 2 * self.inflight + _TIMEOUT_SLACK:
            self._timeouts = [x for x in self._timeouts if self._timing(x[2], x[3])]
            heapq.heapify(self._timeouts)

    def _close(self, sub: Submission, now: float) -> Any:
        """Close ``sub``: release in-flight counts and busy time; returns the unit's state."""
        sub.open = False
        if self._resources is not None:
            for item in sub.items:
                self._resources.move(item, None)
        st = self.state[id(sub.unit)]
        self.subs.pop(sub.id, None)
        st.subs.pop(sub.id, None)
        st.inflight -= len(sub.items)
        self.inflight -= len(sub.items)
        if not st.subs and st.busy_since is not None:
            st.busy_total += now - st.busy_since
            st.busy_since = None
        return st


def _place(item: Any) -> tuple:
    """(job key, address, order key) of an item a statement owns; None parts otherwise."""
    owner = item.owner
    instr = getattr(owner, "instr", None)
    return (item.ext.get("job_key"), getattr(instr, "addr", None), getattr(owner, "key", None))
