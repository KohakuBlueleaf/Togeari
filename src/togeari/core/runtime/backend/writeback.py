"""Writeback: results fill slots and wake waiters (spec/03 §3.8)."""

import time
from typing import Any

from togeari.core.contracts.digest import digest
from togeari.core.contracts.results import Blocked, Cancelled, ItemError, Ok, UnitError
from togeari.core.contracts.workitem import WorkItem
from togeari.core.lanes.deadline import replace as replace_deadline
from togeari.core.plugin.base import PluginBlockError
from togeari.core.runtime.kir import RuntimeFault
from togeari.core.runtime.state import (
    Inflight,
    InfState,
    Slot,
    SlotState,
    Strand,
    StrandState,
    hold,
)
from togeari.core.runtime.values import kept_copy, value_bytes


class UndeclaredMutation(RuntimeError):
    """A node without `mutates` changed one of its inputs (``fingerprint_inputs``)."""


class WritebackStage:
    """Slots, waiters and completions (mixed into ``Dispatcher``)."""

    # ------------------------------------------------------------ slots and waking
    def wake(self, w: Any) -> None:
        match w:
            case Strand() if w.state in (StrandState.PARKED, StrandState.STALLED):
                w.state = StrandState.RUNNABLE
                w.job.mark_runnable(w)
                self.dirty[w.job.id] = None
                if self.bus.wants("strand.resume", w.job.id):
                    self.bus.emit("strand.resume", job=w.job.id, strand=w.id, addr=w.pc)
            case Inflight() if w.state == InfState.WAITING:
                self.check(w)

    def fill(self, slot: Slot, value: Any) -> None:
        slot.value = value
        slot.state = SlotState.READY
        waiters, slot.waiters = slot.waiters, []
        for w in waiters:
            self.wake(w)

    def resolve_phi(self, phi: Slot, target: Slot) -> None:
        phi.forward = target
        t = target
        while t.forward is not None:
            t = t.forward

        # Pending `mutates` versions and waiters move to the resolved slot
        if phi.muts:
            t.muts.extend(phi.muts)
            phi.muts = []
        waiters, phi.waiters = phi.waiters, []
        if t.state != SlotState.PENDING:
            for w in waiters:
                self.wake(w)
        else:
            t.waiters.extend(waiters)

    # ------------------------------------------------------------ lane hooks
    def lane_done(self, lane: Any, sub: Any, results: list) -> None:
        """Called on any thread by engines; becomes an inbox event."""
        self.inbox.put(("done", lane, sub, list(results)))

    def lane_woke(self, lane: Any) -> None:
        """A unit of ``lane`` gained capacity without a completion (any thread)."""
        self.inbox.put(("call", lane.capacity_rose))

    def before_submit(self, lane: Any, unit: Any, items: list) -> list:
        """`pre_submit` plugin hook; blocked items fail as `ItemError(Blocked)`."""
        if not self.plugins.has("pre_submit"):
            return items
        kept = []
        for it in items:
            inf = it.owner
            identity = (
                inf._identity_fields()
                if inf is not None
                else (it.job_execution_id, it.invocation_id, it.attempt_id)
            )
            where = {"lane": lane.name, "entry": inf.job.entry.name if inf is not None else None}
            try:
                new = self.plugins.transform(
                    "pre_submit", it, kind=WorkItem, where=where, unit=unit, lane=lane.name
                )
                if type(new) is not WorkItem:
                    raise PluginBlockError(
                        "pre_submit replacement must be an exact WorkItem; use ext for plugin data"
                    )
            except PluginBlockError as exc:
                it.owner = inf
                it._set_identity(*identity)
                it.ext["settled"] = True
                self.deliver(it, ItemError(Blocked(str(exc))))
                continue

            # Restore authoritative transport even when a plugin mutates the same object.
            it.owner = inf
            it._set_identity(*identity)
            new.owner = inf
            if new is not it:
                replace_deadline(it, new)
                if inf is not None:
                    inf.item = new
            new._set_identity(*identity)
            kept.append(new)
        return kept

    # ------------------------------------------------------------ completions
    def deliver(self, item: WorkItem, result: Any, unit: Any = None) -> None:
        inf = item.owner
        if inf is None:
            return

        # The result event, with queue and service times
        if self.bus.wants("lane.result", inf.job.id):
            sent = item.ext.get("sent")
            queued = sent - item.ext["enqueued"] if sent and "enqueued" in item.ext else None
            self.bus.emit(
                "lane.result",
                job=inf.job.id,
                node=item.node,
                addr=inf.instr.addr,
                lane=inf.instr.binding.lane.name,
                unit=item.ext.get("unit"),
                queued_s=queued,
                service_s=(self.clock() - sent) if sent else None,
                result=type(result).__name__,
                **(
                    {"policy_failure": dict(item.ext["policy_failure"])}
                    if "policy_failure" in item.ext
                    else {}
                ),
            )

        # A cancel the machine did not ask for fails the statement
        try:
            match result:
                case Cancelled():
                    if inf.state != InfState.SQUASHED:
                        err = RuntimeError(f"{item.node}: cancelled")
                        self.complete(inf, ItemError(err), unit)
                case _:
                    self.complete(inf, result, unit)
        except Exception as exc:  # noqa: BLE001
            self.job_error(inf.job, exc)

    def complete(self, inf: Inflight, result: Any, unit: Any) -> None:
        """Writeback: fill output slots and versions, or take the item's failure."""
        job = inf.job
        if inf.state == InfState.SQUASHED or job.ended:
            self.stats["wasted"] += 1
            return
        if inf.state not in (InfState.ISSUED, InfState.READY):
            return
        ins = inf.instr
        if self.plugins.has("post_result"):
            lane = None if ins.binding.inline else ins.binding.lane.name
            result = self.plugins.transform(
                "post_result",
                result,
                kind=(Ok, ItemError, UnitError, Cancelled),
                where={"entry": job.entry.name, "lane": lane},
                node=ins.node,
                job=job.key,
            )
        self.dirty[job.id] = None

        # The item's failure, or its value
        match result:
            case ItemError(error=err) | UnitError(error=err):
                self.fault(inf, err, True)
                return
            case Cancelled():
                self.fault(inf, RuntimeError(f"{ins.node}: cancelled"), True)
                return
            case Ok(value=value, finished=finished):
                pass
            case other:
                self.fault(inf, TypeError(f"{ins.node}: result {other!r}"), True)
                return

        # Decode the raw reply when the engine did not
        if not finished and inf.item is not None:
            try:
                value = inf.item.finish(value)
            except Exception as exc:  # noqa: BLE001
                self.fault(inf, exc, True)
                return

        # Debug check: a node without `mutates` left its inputs unchanged
        fingerprint = self.knobs["fingerprint_inputs"] and not ins.mutates and inf.dig is not None
        if fingerprint and digest(inf.values).hex() != inf.dig:
            self.fault(inf, UndeclaredMutation(f"{ins.node} changed an input"), True)
            return
        if self.bind_outputs(inf, value, unit):
            self.settle(inf, value)

    def bind_outputs(self, inf: Inflight, value: Any, unit: Any) -> bool:
        """Fill output slots like the reference interpreter, then `mutates` versions; False
        after a fault on an output count mismatch."""
        ins = inf.instr
        outs = ins.outs
        if outs:
            if ins.n_sig_out == 1 or len(outs) == 1:
                vals = [value]
            else:
                try:
                    vals = list(value)
                except TypeError:
                    vals = []
                if len(vals) != len(outs):
                    msg = f"{ins.node} returned {len(vals)} values for {len(outs)} outputs"
                    self.fault(inf, RuntimeFault(msg), False)
                    return False
            job = inf.job
            held = 0
            for slot, v in zip(inf.outs, vals, strict=False):
                if slot is not None:
                    self.fill(slot, v)
                    held += hold(slot, job, value_bytes(v))

            # Bytes the filled slots hold count toward the job and the pool while they live
            if held:
                self.pool_bytes += held
                job.stats["bytes_produced"] += held
                if self.freed:
                    self.release_freed()
                if job.bytes > job.stats["bytes_peak"]:
                    job.stats["bytes_peak"] = job.bytes

        # `mutates` versions: the changed object (or the returned one from a copying unit)
        if inf.versions:
            shares = ins.binding.inline or getattr(unit, "shares_memory", True)
            names = [p.name for p in ins.signature.outputs]
            for port, (_, version) in inf.versions.items():
                if shares or port not in names:
                    self.fill(version, inf.values[port])
                else:
                    j = names.index(port)
                    self.fill(version, value if len(names) == 1 else list(value)[j])
        return True

    def settle(self, inf: Inflight, value: Any) -> None:
        """A bound statement is done: record and store copies, counters, store, event."""
        job = inf.job
        ins = inf.instr

        # Recorded jobs, and positional results stored at retire, keep copies no later in-place
        # change reaches; an uncopyable positional result is not stored
        inf.result = value
        positional = inf.store_key is not None and not inf.hit and ins.binding.store == "positional"
        if job.record is not None or positional:
            inf.kept, ok = kept_copy(value)
            if not ok and job.record is not None:
                job.stats["record_uncopied"] = job.stats.get("record_uncopied", 0) + 1
            if not ok and positional:
                inf.store_key = None
                self.stats["store_uncopied"] = self.stats.get("store_uncopied", 0) + 1
            if inf.versions and job.record is not None:
                inf.mutated = {p: kept_copy(inf.values[p])[0] for p in inf.versions}

        # The work item lets go of its inputs and built payload
        if inf.item is not None:
            inf.item.release()

        if inf.state == InfState.ISSUED:
            job.issued -= 1
        inf.state = InfState.DONE
        inf.t_done = time.perf_counter()
        job.unfinished -= 1
        if (
            inf.store_key is not None
            and not inf.hit
            and ins.binding.store == "content"
            and self.result_store_allowed(job, ins)
        ):
            self.store_put(inf.store_key, value)
        if self.bus.wants("stmt.done", job.id):
            match inf:
                case Inflight(item=WorkItem() as it):
                    where = it.ext.get("unit")
                case Inflight(hit=True):
                    where = "store"
                case Inflight(reused=True):
                    where = "reused"
                case _:
                    where = "inline"
            self.emit_stmt("stmt.done", inf, unit=where)
