"""Issue: ready statements to inline runs, store hits, or lane work items (spec/03 §3.7)."""

import logging
import time
from collections import deque
from typing import Any

from togeari.core.contracts.digest import digest, digest_with_weak
from togeari.core.contracts.io import MISS, copy_value
from togeari.core.contracts.results import ItemError, Ok
from togeari.core.contracts.views import ReadyView
from togeari.core.contracts.workitem import WorkItem
from togeari.core.runtime.backend.persistence import result_execution
from togeari.core.runtime.kir import UNDEF, RuntimeFault
from togeari.core.runtime.state import Inflight, InfState, SlotState, effective

log = logging.getLogger("togeari.dispatcher")

# Inline timing: the first median check, then one every this many samples
_FIRST_CHECK, _CHECK_EVERY = 32, 1024


class Gate:
    """What an `ordered` / `mutates` statement does now."""

    GO = "go"
    COPY = "copy"
    WAIT = "wait"


class IssueStage:
    """Inflights from fetch to issue (mixed into ``Dispatcher``)."""

    # ------------------------------------------------------------ readiness
    def admit_inflight(self, inf: Inflight) -> None:
        job = inf.job
        job.add(inf)
        job.unfinished += 1
        job.stats["calls"] += 1
        self.stats["calls"] += 1
        if self.bus.wants("stmt.fetch", job.id):
            self.emit_stmt("stmt.fetch", inf)
        self.check(inf)

    def check(self, inf: Inflight) -> None:
        """Ready when every input (through phis and older `mutates` versions) is filled."""
        for _, slot in inf.inputs:
            s = effective(slot, inf.key)
            match s.state:
                case SlotState.PENDING:
                    s.waiters.append(inf)
                    return
                case SlotState.SQUASHED:
                    return
        if inf.collector is not None:
            inf.state = InfState.DONE
            self.dirty[inf.job.id] = None
            return
        inf.state = InfState.READY
        self.ready.append(inf)
        if self.bus.wants("stmt.ready", inf.job.id):
            self.emit_stmt("stmt.ready", inf)

    def gate(self, inf: Inflight) -> str:
        """Go, copy or wait, by older unfinished work and older live strands of the job."""
        ins = inf.instr
        job = inf.job
        open_key = job.oldest_open_key()
        live_key = job.oldest_live_key()
        speculative = (open_key is not None and open_key < inf.key) or (
            live_key is not None and live_key < inf.key
        )
        if ins.ordered and speculative:
            return Gate.WAIT
        if ins.mutates:
            match self.knobs["mutates"]:
                case "copy-always":
                    return Gate.COPY
                case "wait" if speculative:
                    return Gate.WAIT
                case _ if speculative:
                    return Gate.COPY
        return Gate.GO

    # ------------------------------------------------------------ issue
    def issue(self) -> None:
        """Issue the ready statements in the order the issue policy gives their views."""
        batch, self.ready = self.ready, []
        if not batch:
            return
        for inf in self.issue_order(batch):
            if inf.state != InfState.READY:
                continue
            job = inf.job
            if job.paused:
                job.held.append(inf)
                continue
            try:
                self.issue_one(inf)
            except Exception as exc:  # noqa: BLE001
                self.job_error(job, exc)

    def issue_order(self, batch: list[Inflight]) -> list[Inflight]:
        """The policy's order of ``batch``, each statement once, then the ones it left out in
        arrival order; a raising policy gives arrival order."""
        views = [ReadyView(i) for i in batch]
        mine = {id(v): v._inf for v in views}
        try:
            chosen = list(self.knobs["issue"].order(views))
        except Exception as exc:  # noqa: BLE001
            self.policy_failed("issue order", exc)
            chosen = views

        # Views of this batch only, each once
        ordered = []
        seen: set[int] = set()
        for view in chosen:
            inf = mine.get(id(view))
            if inf is None or id(inf) in seen:
                continue
            seen.add(id(inf))
            ordered.append(inf)
        ordered += [inf for inf in batch if id(inf) not in seen]
        return ordered

    def issue_priority(self, inf: Inflight) -> int:
        """The policy's lane priority for a statement; 0 when the policy raises."""
        try:
            return self.knobs["issue"].priority(ReadyView(inf))
        except Exception as exc:  # noqa: BLE001
            self.policy_failed("issue priority", exc)
            return 0

    def policy_failed(self, what: str, exc: BaseException) -> None:
        """A user policy raised: counted in ``machine_errors`` and ``policy_errors``, logged at
        the 1st, 2nd, 4th, 8th ... error."""
        self.stats["machine_errors"] += 1
        n = self.stats["policy_errors"] = self.stats.get("policy_errors", 0) + 1
        if n & (n - 1) == 0:
            log.error("%s policy failed (%d so far); fallback used", what, n, exc_info=exc)

    def issue_one(self, inf: Inflight) -> None:
        ins = inf.instr
        job = inf.job

        # Effects and in-place changes wait for (or copy around) older work
        g = Gate.GO
        if ins.ordered or ins.mutates:
            g = self.gate(inf)
            if g == Gate.WAIT:
                self.blocked.setdefault(job.id, []).append(inf)
                return

        # Input values as this statement sees them
        values: dict[str, Any] = {}
        for port, slot in inf.inputs:
            v = effective(slot, inf.key).value
            if v is UNDEF:
                self.fault(inf, RuntimeFault(f"{ins.node}: input {port!r} is not defined"), False)
                return
            values[port] = v

        # Copies for `mutates` ports when older work may still fail
        if g == Gate.COPY:
            for port in inf.versions:
                values[port] = copy_value(values[port])
            inf.copy = True
            job.stats["mutates_copies"] += 1
        inf.values = values

        # The inputs digest: for the record and the fingerprint check, and to find a result kept
        # from a squashed run of this statement
        need = job.record is not None or self.knobs["fingerprint_inputs"]
        kept = job.memo is not None and ins.addr in job.memo
        if need or kept:
            dig, weak = digest_with_weak(values)
            if need:
                inf.dig = dig.hex()
                if weak:
                    self.note_weak(job, weak)
            if kept and not weak and self.take_reused(inf, dig):
                return
        self.execute(inf)

    def execute(self, inf: Inflight) -> None:
        ins = inf.instr
        b = ins.binding
        inf.t_issue = time.perf_counter()

        # Inline: run on the dispatcher now
        if b.inline:
            t0 = time.perf_counter()
            try:
                res: Any = Ok(b.fn(**inf.values), True)
            except Exception as exc:  # noqa: BLE001
                res = ItemError(exc)
            self.note_inline(ins.node, time.perf_counter() - t0, b.warn_us)
            inf.job.issue(inf)
            self.complete(inf, res, None)
            return

        # Store lookup
        if b.store is not None and self.store is not None:
            inf.store_key = self.store_key(inf)
            if inf.store_key is not None:
                hit = self.store_get(inf.store_key)
                eligible = self.result_store_allowed(inf.job, ins)
                if not eligible:
                    inf.store_key = None
                if hit is not MISS and eligible:
                    self.stats["store_hits"] += 1
                    inf.hit = True
                    inf.job.issue(inf)
                    self.complete(inf, Ok(hit, True), None)
                    return
                self.stats["store_misses"] += 1

        # Lane: the call budget begins before item preparation and policy callbacks.
        now = self.clock()
        item = WorkItem(
            ins.node,
            inf.values,
            b.adapter,
            protocol=self.m.defaults.protocol,
            priority=self.issue_priority(inf),
            owner=inf,
        )
        timeout = b.policy.timeout_s
        if timeout is not None:
            item.deadline = now + timeout
            item.ext["call_timeout_s"] = timeout
            item.ext["policy_source"] = b.policy_source
        item.ext["job_key"] = inf.job.key
        item.ext["job"] = inf.job.id
        item.ext["hint"] = ins.hint
        if ins.ordered and not b.idempotent:
            item.ext["ordered_once"] = True
        inf.item = item
        inf.job.issue(inf)
        b.lane.enqueue(item, now)
        jid = inf.job.id
        if self.bus.wants("stmt.issue", jid):
            self.emit_stmt("stmt.issue", inf)
        if self.bus.wants("lane.enqueue", jid):
            self.bus.emit("lane.enqueue", job=jid, lane=b.lane.name, node=ins.node)

    # ------------------------------------------------------------ stores
    def store_key(self, inf: Inflight) -> bytes | None:
        """The store key of a statement; None when its inputs (content) or the job's inputs
        (positional) digest only by type (weak)."""
        ins = inf.instr
        b = ins.binding
        region = self.code.region_of(ins.addr)
        execution_hash = result_execution(region, self.code, self.m, inf.job.stats)
        if execution_hash is None:
            self.stats["store_misses"] += 1
            return None

        match b.store:
            # By the inputs' content
            case "content":
                dig, weak = digest_with_weak(inf.values)
                if weak:
                    self.stats["store_weak"] += 1
                    self.note_weak(inf.job, weak)
                    return None
                key = digest(("togeari.result", 2, "content", execution_hash, ins.node, dig))

            # By the job and the statement's place in it
            case "positional":
                job = inf.job
                self.warn_auto_key(job)
                if job.inputs_dig is None:
                    self.stats["store_weak"] += 1
                    return None
                # The statement's address in its region and its order key name one call
                offset = ins.addr - region.start
                key = digest(
                    (
                        "togeari.result",
                        2,
                        "positional",
                        execution_hash,
                        job.key,
                        job.inputs_dig,
                        offset,
                        inf.key,
                    )
                )
            case _:
                return None
        # Digests can invoke user hooks; never use a key from a changed history.
        if not self.result_store_allowed(inf.job, ins):
            self.stats["store_misses"] += 1
            return None
        return key

    def result_store_allowed(self, job, ins) -> bool:
        """Recheck history at writeback/retire without re-digesting potentially mutated inputs."""
        region = self.code.region_of(ins.addr)
        return result_execution(region, self.code, self.m, job.stats) is not None

    def store_get(self, key: bytes) -> Any:
        try:
            return self.store.get(key)
        except Exception as exc:  # noqa: BLE001
            self.store_error("get", exc)
            return MISS

    def store_put(self, key: bytes, value: Any) -> None:
        try:
            self.store.put(key, value)
        except Exception as exc:  # noqa: BLE001
            self.store_error("put", exc)

    def store_delete(self, key: bytes) -> None:
        fn = getattr(self.store, "delete", None)
        try:
            if fn is not None:
                fn(key)
            else:
                self.store.put(key, None)
        except Exception as exc:  # noqa: BLE001
            self.store_error("delete", exc)

    def store_error(self, op: str, exc: BaseException) -> None:
        self.stats["store_errors"] += 1
        log.warning("store %s failed: %s", op, exc, exc_info=exc)
        if self.bus.wants("store.error"):
            self.bus.emit("store.error", op=op, error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ reports
    def note_inline(self, node: str, dt: float, warn_us: float | None) -> None:
        stat = self.inline.get(node)
        if stat is None:
            stat = self.inline[node] = InlineStat()
            self.stats["inline_s"][node] = stat.times
        stat.add(dt)
        limit = warn_us if warn_us is not None else self.knobs["inline_warn_us"]
        if limit is None or stat.warned:
            return
        if stat.n == _FIRST_CHECK or stat.n % _CHECK_EVERY == 0:
            med = stat.median()
            if med * 1e6 > limit:
                stat.warned = True
                log.warning("inline node %s median %.1f us > %s us", node, med * 1e6, limit)

    def note_weak(self, job: Any, weak: list[str]) -> None:
        """Values compared by type only (unpicklable): counted per job and per machine."""
        mine = job.stats.setdefault("weak_digests", {})
        seen = self.stats["weak_digests"]
        for t in weak:
            mine[t] = mine.get(t, 0) + 1
            if t not in seen:
                log.warning("values of type %s digest by type only (weak comparison)", t)
            seen[t] = seen.get(t, 0) + 1

    def emit_stmt(self, kind: str, inf: Inflight, **extra: Any) -> None:
        ins = inf.instr
        b = ins.binding
        lane = None if b is None or b.inline else b.lane.name
        self.bus.emit(
            kind, job=inf.job.id, node=ins.node, addr=ins.addr, key=inf.key, lane=lane, **extra
        )


class InlineStat:
    """Run times of one inline node: the last 1000, the count, whether it was reported."""

    __slots__ = ("times", "n", "total", "warned")

    def __init__(self) -> None:
        self.times: deque = deque(maxlen=1000)
        self.n = 0
        self.total = 0.0
        self.warned = False

    def add(self, dt: float) -> None:
        self.times.append(dt)
        self.n += 1
        self.total += dt

    def median(self) -> float:
        s = sorted(self.times)
        return s[len(s) // 2] if s else 0.0
