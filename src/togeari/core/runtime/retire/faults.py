"""Precise faults: mark, squash everything younger, take in program order (spec/03 §3.9).

Completed calls the squash drops keep their results for their refetch (``squash_reuse``).
"""

from togeari.core.contracts.digest import digest_with_weak
from togeari.core.contracts.results import Ok
from togeari.core.program.decode.instr import Op
from togeari.core.runtime.backend.persistence import memo_execution
from togeari.core.runtime.identity import record_invocation
from togeari.core.runtime.kir import UNDEF, RuntimeFault
from togeari.core.runtime.state import (
    Inflight,
    InfState,
    Job,
    NameMap,
    SlotState,
    Strand,
    const,
    effective,
)
from togeari.core.runtime.values import value_bytes

_LIVE = (InfState.WAITING, InfState.READY, InfState.ISSUED)


class FaultStage:
    """Faults and squashes (mixed into ``Dispatcher``)."""

    def fault(self, inf: Inflight, exc: BaseException, catchable: bool) -> None:
        job = inf.job
        if inf.state in _LIVE and not inf.control:
            job.unfinished -= 1
        if inf.state == InfState.ISSUED:
            job.issued -= 1
        kind = "KirRuntimeError" if isinstance(exc, RuntimeFault) else type(exc).__name__
        inf.fault = {
            "error": {"kind": kind, "node": inf.instr.node, "message": str(exc)},
            "catchable": catchable and not isinstance(exc, RuntimeFault),
        }
        inf.state = InfState.DONE
        self.dirty[job.id] = None

        # Only a fault a handler takes refetches the squashed code; others end the job
        handled = inf.fault["catchable"] and inf.instr.try_id is not None
        self.squash_younger(job, inf.key, keep=handled)

    def squash_younger(self, job: Job, key: tuple, keep: bool = False) -> None:
        """Squash every younger statement and strand; with ``keep``, completed results stay
        for the refetch (``squash_reuse``)."""
        cap = self.knobs["squash_reuse"] if keep else 0
        for x in job.window.cut_after(key):
            if cap:
                self.keep_for_reuse(x, cap, self.knobs["squash_reuse_bytes"])
            self.squash(x)
        for st in list(job.strands.values()):
            if st.next_key() > key:
                job.end_strand(st)

        # A younger strand that already reached the end no longer counts
        if job.final_key is not None and job.final_key > key:
            job.final_names = None
            job.final_key = None

    def memo_reuse_allowed(self, inf: Inflight) -> bool:
        """Determinism is tied to this admitted execution contract, not later lane changes."""
        if not inf.instr.binding.deterministic:
            return False
        job = inf.job
        return memo_execution(job.entry.region, self.code, self.m, job.stats)

    def keep_for_reuse(self, x: Inflight, cap: int, cap_bytes: int | None) -> None:
        """Keep the result of a completed call the squash drops, by address and inputs digest,
        for its refetch; at most ``cap`` entries and ``cap_bytes`` bytes per job (None: no
        byte cap), the oldest dropped first; a result larger than ``cap_bytes`` is not kept.

        Kept: calls without `ordered` or `mutates` whose binding is not positional (a positional
        result belongs to its order key, which a refetch after a handler does not keep).
        """
        ins = x.instr
        reusable = (
            x.state == InfState.DONE
            and x.fault is None
            and ins.op == Op.CALL
            and not ins.ordered
            and not ins.mutates
            and ins.binding.store != "positional"
            and ins.binding.deterministic
            and x.values is not None
        )
        if not reusable or not self.memo_reuse_allowed(x):
            return
        size = value_bytes(x.result)
        if cap_bytes is not None and size > cap_bytes:
            return
        dig, weak = digest_with_weak(x.values)
        if weak or not self.memo_reuse_allowed(x):
            return

        # The entry, and its bytes on the job until it is used or dropped
        job = x.job
        if job.memo is None:
            job.memo = Memo()
        memo = job.memo
        memo.add(ins.addr, (dig, x.result, size))
        job.bytes += size
        self.pool_bytes += size

        # Over a cap: the oldest address's oldest entry goes
        while memo.n > cap or (cap_bytes is not None and memo.bytes > cap_bytes):
            self.memo_dropped(job, memo.pop_oldest())

    def memo_dropped(self, job: Job, size: int) -> None:
        """``size`` bytes of memo entries left the job."""
        job.bytes -= size
        self.pool_bytes -= size

    def take_reused(self, inf: Inflight, dig: bytes) -> bool:
        """Complete ``inf`` with a kept result of the same statement and inputs; False if none.
        Each kept result serves one refetch."""
        job = inf.job
        if not self.memo_reuse_allowed(inf):
            return False
        found = job.memo.take(inf.instr.addr, dig)
        if found is None:
            return False

        # The entry left the memo; its value goes through writeback like a store hit
        value, size = found
        self.memo_dropped(job, size)
        inf.reused = True
        inf.resolution = "squash_reuse"
        job.stats["reused"] += 1
        self.stats["reused"] += 1
        job.issue(inf)
        self.complete(inf, Ok(value, True), None)
        return True

    def squash(self, x: Inflight) -> None:
        """Squash one inflight: counts, its lane item, its output and version slots; it lets go
        of its slots and values (a lane may still hold its work item)."""
        job = x.job
        if x.state in _LIVE and not x.control:
            job.unfinished -= 1
        if x.state == InfState.ISSUED:
            job.issued -= 1

        # Its lane item is dropped from the queue, or its submission cancelled
        b = x.instr.binding
        if x.item is not None and b is not None and not b.inline:
            b.lane.cancel(x.item)

        # Its output and version slots are squashed
        x.state = InfState.SQUASHED
        if x.collector is not None and x.collect_index is None:
            x.collector.queued = False
        x.collector = None
        for s in x.outs:
            if s is not None:
                s.state = SlotState.SQUASHED
        if x.versions:
            for _, version in x.versions.values():
                version.state = SlotState.SQUASHED

        job.stats["squashed"] += 1
        self.stats["squashed"] += 1
        if self.bus.wants("stmt.squash", job.id):
            self.emit_stmt("stmt.squash", x)
        x.values = x.result = x.snapshot = x.kept = x.mutated = x.versions = x.item = None
        x.inputs = x.outs = ()

    def take_fault(self, job: Job, x: Inflight) -> None:
        job.window.pop_first()
        x.state = InfState.RETIRED
        err = x.fault["error"]
        if not x.control and job.record is not None:
            x.instr.record_origin(job.stats.setdefault("record_origins", []), len(job.record))
            record_invocation(
                job.stats.setdefault("record_invocations", []),
                len(job.record),
                x.invocation_id,
                x.attempt_ids,
                x.resolution,
            )
            job.record.append((x.instr.node, x.dig, "error", err, None))
            limit = self.knobs["record_limit"]
            if limit is not None and len(job.record) > limit:
                job.record = None
                job.stats.pop("record_origins", None)
                job.stats.pop("record_invocations", None)
                job.stats["record_skipped"] = f"more than {limit} calls"
        origin = x.instr.origin_dict()
        policy = x.item.ext.get("policy_failure") if x.item is not None else None
        policy_fields = {}
        if policy is not None:
            policy = dict(policy)
            policy_fields["policy_failure"] = policy
            job.stats.setdefault("policy_failures", []).append(
                {
                    **policy,
                    "node": x.instr.node,
                    "addr": x.instr.addr,
                    "key": x.key,
                    "record_index": len(job.record) - 1 if job.record is not None else None,
                    "origin": origin,
                }
            )
        job.stats["fault_origin"] = origin
        info = {"job": job.id, "key": job.key, "origin": origin, **err, **policy_fields}
        self.plugins.notify("on_fault", info, where=self.where(job))
        if self.bus.wants("fault.taken", job.id):
            self.bus.emit(
                "fault.taken",
                job=job.id,
                addr=x.instr.addr,
                key=x.key,
                squashed=job.stats["squashed"],
                origin=origin,
                **policy_fields,
                **{f"error_{k}": v for k, v in err.items()},
            )
        names, frames, prefix, counter = x.snapshot

        # The taken statement lets go of its values, work item, view and slots
        x.values = x.result = x.snapshot = x.kept = x.mutated = x.versions = x.item = None
        x.inputs = x.outs = ()

        # Inside a `@try`: continue at the handler with the faulting statement's view
        tid = x.instr.try_id
        if x.fault["catchable"] and tid is not None:
            info = self.code.tries[tid]
            tables = self.code.tables(info.handler)

            # Younger continuations were squashed: the handler strand carries on past each join
            kept = []
            for f in frames:
                if f.branch is not None and f.branch in info.body:
                    continue
                if f.join is not None:
                    reconv = tables.reconv.get(f.each) if f.kind == "iter" else f.reconv
                    f = f.replace(join=None, reconv=reconv)
                kept.append(f)
            # Only collectors surrounding the local handler survive a precise fault.
            surviving = {f.collector.key for f in kept if f.collector is not None}
            for key, collector in list(job.collectors.items()):
                if key not in surviving:
                    collector.abort()
                    del job.collectors[key]
                else:
                    self.front.collect_finalize(job, collector)
            nm = NameMap.of(names)
            if info.error_name:
                nm.set(info.error_name, const(dict(err)))
            job.add_strand(Strand(job, info.handler, prefix, counter, tuple(kept), nm))
            self.dirty[job.id] = None
            return

        # Outside any `@try`: the job fails with the outputs written so far
        outputs, missing = {}, []
        for o in job.entry.outputs or []:
            s = names.get(o)
            v = effective(s, x.key) if s is not None else None
            if v is not None and v.state == SlotState.READY and v.value is not UNDEF:
                outputs[o] = v.value
            else:
                missing.append(o)
        job.failure = err
        self.end_job(job, outputs, missing)


class Memo:
    """A job's results kept from squashed statements: entries ``(inputs digest, result, bytes)``
    by address in insertion order, with their count and bytes."""

    __slots__ = ("entries", "n", "bytes")

    def __init__(self) -> None:
        self.entries: dict[int, list] = {}
        self.n = 0
        self.bytes = 0

    def __contains__(self, addr: int) -> bool:
        return addr in self.entries

    def add(self, addr: int, entry: tuple) -> None:
        self.entries.setdefault(addr, []).append(entry)
        self.n += 1
        self.bytes += entry[2]

    def pop_oldest(self) -> int:
        """Drop the oldest address's oldest entry; its bytes."""
        addr = next(iter(self.entries))
        return self._remove(addr, 0)

    def take(self, addr: int, dig: bytes) -> tuple | None:
        """Remove the entry of ``addr`` with inputs digest ``dig``: (result, bytes), or None."""
        for i, (d, value, _) in enumerate(self.entries.get(addr, ())):
            if d == dig:
                return value, self._remove(addr, i)
        return None

    def _remove(self, addr: int, i: int) -> int:
        entries = self.entries[addr]
        _, _, size = entries.pop(i)
        if not entries:
            del self.entries[addr]
        self.n -= 1
        self.bytes -= size
        return size
