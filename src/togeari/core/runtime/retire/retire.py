"""Retire in order-key order; finish a job; snapshot at retire boundaries (spec/03 §3.9, §3.10)."""

import logging

from togeari.core.program.decode.instr import Op
from togeari.core.program.execution import compatibility, execution_for_job
from togeari.core.runtime.identity import record_invocation
from togeari.core.runtime.kir import UNDEF, RuntimeFault
from togeari.core.runtime.retire import snapshot
from togeari.core.runtime.state import Inflight, InfState, Job, SlotState, deref, effective, hold
from togeari.core.runtime.values import value_bytes

log = logging.getLogger("togeari.dispatcher")
_FINAL = (float("inf"),)


class RetireStage:
    """Retire, finish, snapshots (mixed into ``Dispatcher``)."""

    def retire(self, job: Job) -> None:
        w = job.window
        retired = 0
        bound = job.oldest_live_key()
        while w and not job.ended:
            x = w.first()
            if x.state != InfState.DONE:
                break
            if bound is not None and x.key >= bound:
                break
            if x.collector is not None and x.collect_index is not None and x.fault is None:
                value = effective(x.inputs[0][1], x.key).value
                if value is UNDEF:
                    self.fault(x, RuntimeFault("collect: yielded value is not defined"), False)
            if x.fault is not None:
                self.take_fault(job, x)
                bound = job.oldest_live_key()
                continue
            w.pop_first()
            x.state = InfState.RETIRED
            job.checkpoint_watermark = x.key
            retired += 1
            self.retire_one(job, x)
            if x.versions:
                fold_versions(x)

            # The retired statement lets go of its values, work item, view and slots
            x.values = x.result = x.snapshot = x.kept = x.mutated = x.versions = x.item = None
            x.inputs = x.outs = ()
        job.oldest_open_key()
        job.stats["retired"] += retired
        if job.ended:
            return

        # Snapshot at a retire boundary every `snapshot_every` retired statements
        every = self.knobs["snapshot_every"]
        due = every and job.stats["retired"] - job.snap_at >= every
        if due and self.store is not None and not job.snap_off:
            self.take_snapshot(job)

        # Statements blocked on older work may go now; strands at a cap may fetch again
        blocked = self.blocked.pop(job.id, None)
        if blocked:
            for inf in blocked:
                if inf.state == InfState.READY:
                    self.ready.append(inf)
        if retired and job.runnable:
            self.dirty[job.id] = None
        if job.cut is not None:
            self.progress_checkpoint(job)
        if not w and not job.strands:
            self.finish(job)

    def retire_one(self, job: Job, x: Inflight) -> None:
        ins = x.instr
        if x.collector is not None:
            collector = x.collector
            if not collector.active:
                return
            if x.collect_index is None:
                if len(collector.values) != collector.count:
                    raise RuntimeError("collect: incomplete retirement")
                value = collector.values
                collector.values = []
                self.fill(collector.output, value)
                self.hold_collect_slot(job, collector.output, value_bytes(value))
                job.stats["collect_finalizations"] = job.stats.get("collect_finalizations", 0) + 1
                collector.slots.clear()
                collector.snapshot = None
                collector.active = False
                job.collectors.pop(collector.key, None)
            else:
                if x.collect_index != len(collector.values):
                    raise RuntimeError("collect: out-of-order retirement")
                slot = effective(x.inputs[0][1], x.key)
                collector.values.append(slot.value)
                collector.slots.append(slot)
                self.hold_collect_slot(job, slot, value_bytes(slot.value))
                self.hold_collect_slot(job, collector.output, 64 + 16 * len(collector.values))
                job.stats["collect_captures"] = job.stats.get("collect_captures", 0) + 1
                job.stats["collect_peak_items"] = max(
                    job.stats.get("collect_peak_items", 0), len(collector.values)
                )
            x.collector = None
            return
        if self.knobs["assert_alias"]:
            self.alias_check(job, x)
        if job.record is not None and ins.op == Op.CALL:
            ins.record_origin(job.stats.setdefault("record_origins", []), len(job.record))
            record_invocation(
                job.stats.setdefault("record_invocations", []),
                len(job.record),
                x.invocation_id,
                x.attempt_ids,
                x.resolution,
            )
            job.record.append((ins.node, x.dig, "ok", x.kept, x.mutated))
            limit = self.knobs["record_limit"]
            if limit is not None and len(job.record) > limit:
                job.record = None
                job.stats.pop("record_origins", None)
                job.stats.pop("record_invocations", None)
                job.stats["record_skipped"] = f"more than {limit} calls"
        # A positional result is stored as copied at settle, before any younger in-place change
        if (
            x.store_key is not None
            and not x.hit
            and ins.binding.store == "positional"
            and self.result_store_allowed(job, ins)
        ):
            self.store_put(x.store_key, x.kept)
        if self.bus.wants("stmt.retire", job.id):
            self.emit_stmt("stmt.retire", x)

    def hold_collect_slot(self, job: Job, slot, size: int) -> None:
        """Charge retained values and the linear private reference buffer until publication."""
        before = getattr(slot, "size", 0)
        added = max(0, size - before)
        if before:
            slot.size += added
            job.bytes += added
        else:
            added = hold(slot, job, size)
        self.pool_bytes += added
        job.stats["bytes_produced"] += added
        job.stats["bytes_peak"] = max(job.stats["bytes_peak"], job.bytes)

    def take_snapshot(self, job: Job) -> None:
        if job.collectors:
            return
        key = self.snapshot_key(job)
        if key is None:
            job.snap_off = True
            return
        try:
            manifest = execution_for_job(job, self.code, self.m)
            decision = compatibility(manifest, manifest, self.m.persistence_compatibility)
            if not decision["allow"]:
                job.stats["snapshot_capture"] = decision
                job.snap_off = True
                return
            blob = snapshot.capture(job, self.code, manifest)
        except Exception as exc:  # noqa: BLE001
            job.snap_off = True
            msg = f"{type(exc).__name__}: {exc}"
            job.stats["snapshot_error"] = msg
            self.stats["snapshot_errors"] += 1
            log.warning("job %s cannot snapshot: %s", job.key, msg)
            if self.bus.wants("snapshot.error", job.id):
                self.bus.emit("snapshot.error", job=job.id, key=job.key, error=msg)
            return
        if blob is not None:
            try:
                self.store.put(key, blob)
            except Exception as exc:  # noqa: BLE001
                self.store_error("put", exc)
                return
            job.snap_at = job.stats["retired"]
            job.stats["snapshots"] = job.stats.get("snapshots", 0) + 1

    def snapshot_key(self, job: Job) -> bytes | None:
        """The job's snapshot key; None without a store or with weak inputs."""
        if self.store is None or job.inputs_dig is None:
            return None
        region = self.code.region_of(job.entry.addr)
        return snapshot.snapshot_key(region.semantic_hash, job.key, job.inputs_dig)

    # ------------------------------------------------------------ debug checks
    def alias_check(self, job: Job, x: Inflight) -> None:
        """At retire: every input this statement read and every value it wrote is filled."""
        bad = [p for p, s in x.inputs if effective(s, x.key).state != SlotState.READY]
        bad += [
            f"output {i}"
            for i, s in enumerate(x.outs)
            if s is not None and s.state != SlotState.READY
        ]
        if x.versions:
            bad += [f"{p} after" for p, (_, v) in x.versions.items() if v.state != SlotState.READY]
        if bad and job.failure is None:
            job.failure = {
                "kind": "AliasCheck",
                "node": x.instr.node,
                "message": f"at retire, not filled: {bad}",
            }

    def alias_at_end(self, job: Job, final) -> str | None:
        """A problem with the names a job ends with, or None."""
        slots = {n: effective(final.get(n), _FINAL) for n in final}
        squashed = sorted(n for n, s in slots.items() if s.state != SlotState.READY)
        if squashed:
            return f"names not filled at the end: {squashed}"

        # Mutable values of this job reachable from another resident job
        mine = {id(s.value): n for n, s in slots.items() if _mutable(s.value)}
        for other in self.jobs.values():
            if other is job:
                continue
            for v in _values_of(other):
                if id(v) in mine:
                    return f"{mine[id(v)]!r} is shared with job {other.key!r}"
        return None

    # ------------------------------------------------------------ job end
    def finish(self, job: Job) -> None:
        """A job with no live strand and an empty window: collect its outputs."""
        final = job.final_names
        outputs, missing = {}, []
        if final is None:
            names = list(job.entry.outputs or [])
        elif job.entry.outputs is None:
            names = [n for n in final if "__" not in n]
        else:
            names = list(job.entry.outputs)
        for o in names:
            s = final.get(o) if final is not None else None
            v = effective(s, _FINAL) if s is not None else None
            if v is None or v.state != SlotState.READY or v.value is UNDEF:
                missing.append(o)
            else:
                outputs[o] = v.value

        # Debug check: every name filled, no mutable value shared with another resident job
        if self.knobs["assert_alias"] and final is not None and job.failure is None:
            message = self.alias_at_end(job, final)
            if message is not None:
                job.failure = {"kind": "AliasCheck", "node": None, "message": message}
        if missing and job.entry.outputs is not None:
            job.failure = {
                "kind": "KirRuntimeError",
                "node": job.entry.name,
                "message": f"@def {job.entry.name}: outputs {missing} not set",
            }
        self.end_job(job, outputs, missing)


def fold_versions(x: Inflight) -> None:
    """A retired `mutates` call: its version becomes the oldest one its values keep.

    Every reader older than the call has retired, so versions before it and squashed ones
    are dropped; ``effective`` scans only versions of unretired calls.
    """
    for before, _ in x.versions.values():
        base = deref(before)
        base.muts = [(k, v) for k, v in base.muts if k >= x.key and v.state != SlotState.SQUASHED]


def _mutable(v) -> bool:
    return isinstance(v, (list, dict, set, bytearray)) or hasattr(v, "__dict__")


def _values_of(job: Job):
    """Every value a resident job's live names and window hold."""
    for st in job.strands.values():
        if st.names is not None:
            for slot in st.names.to_dict().values():
                s = effective(slot, _FINAL)
                if s.state == SlotState.READY:
                    yield s.value
    for x in job.window:
        if x.values:
            yield from x.values.values()
