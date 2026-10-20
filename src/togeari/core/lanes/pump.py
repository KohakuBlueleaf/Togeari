"""Multi-issue of a lane: every unit with free capacity takes work each pump (spec/04 §4.4)."""

import heapq
import itertools
import logging
import time
from dataclasses import dataclass
from typing import Any

from togeari.core.contracts.results import UnitError
from togeari.core.lanes.health import State

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

        # No unit up: the dead-lane clock runs
        if not up:
            if self.no_unit_since is None:
                self.no_unit_since = now
            return 0
        if self.no_unit_since is not None:
            self.stats["no_unit_s"] += now - self.no_unit_since
            self.no_unit_since = None
        if not self.queued_now():
            return 0

        # Early forming: shared-queue items go to unit queues first
        early = self.forming == "early"
        if early and len(self._q):
            self._spread(up, now)

        # Each unit in selector order, up to its capacity
        sent = 0
        admits = self._admits
        for unit in self._order(up):
            st = self.state[id(unit)]
            queue = st.queue if early else self._q
            if not len(queue):
                continue
            queue.promote(now)
            try:
                cap = unit.capacity()
            except Exception:  # noqa: BLE001
                cap = 0
            while cap > 0 and self.under_cap() and st.state == State.UP:
                items = queue.take(unit, self._count, admits, now)
                if not items:
                    break
                self._submit(unit, items, now)
                sent += len(items)
                cap -= 1
        return sent

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
        # Plugins see (and may block) items after unit choice
        if self.hook_submit is not None:
            items = self.hook_submit(self, unit, items)
            if not items:
                return

        # Submission and in-flight accounting
        sub = Submission(next(_sub_ids), unit, items, sent=now)
        st = self.state[id(unit)]
        self.subs[sub.id] = sub
        st.subs.add(sub.id)
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

        t0 = time.perf_counter()
        try:
            sub.ticket = unit.submit(items if batch else items[0], done)
        except Exception as exc:  # noqa: BLE001
            self.on_done(self, sub, [UnitError(exc)] * len(items))
        st.submit_lat = time.perf_counter() - t0
        # The event carries the first item's job, so job sampling applies to it
        jid = items[0].ext.get("job")
        if self.wants("lane.submit", jid):
            self.emit("lane.submit", job=jid, lane=self.name, unit=unit.name, items=len(items))

    def _arm_timeout(self, item: Any, sub: Submission, now: float) -> None:
        """The item's deadline in flight: ``sent + timeout_s``, or the ``WorkItem.deadline`` it
        carried when queued (the statement's ``@hint timeout_s``), whichever is earlier."""
        ends = [item.ext.get("deadline_cap")]
        if self.timeout_s is not None:
            ends.append(now + self.timeout_s)
        ends = [t for t in ends if t is not None]
        if not ends:
            return
        deadline = min(ends)
        item.ext["timeout_s"] = round(deadline - now, 6)
        item.ext["deadline"] = deadline
        item.deadline = deadline
        heapq.heappush(self._timeouts, (deadline, next(self._seq), item, sub))

        # Entries of settled items are dropped once they outnumber the items in flight
        if len(self._timeouts) > 2 * self.inflight + _TIMEOUT_SLACK:
            self._timeouts = [x for x in self._timeouts if self._timing(x[2], x[3])]
            heapq.heapify(self._timeouts)

    def _close(self, sub: Submission, now: float) -> Any:
        """Close ``sub``: release in-flight counts and busy time; returns the unit's state."""
        sub.open = False
        st = self.state[id(sub.unit)]
        self.subs.pop(sub.id, None)
        st.subs.discard(sub.id)
        st.inflight -= len(sub.items)
        self.inflight -= len(sub.items)
        if not st.subs and st.busy_since is not None:
            st.busy_total += now - st.busy_since
            st.busy_since = None
        return st
