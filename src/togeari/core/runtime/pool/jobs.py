"""The job pool: start, end, records, admission (spec/03 §3.10, spec/05 §5.1)."""

import copy
import logging
import queue
import time
from typing import Any

from togeari.core.contracts.digest import digest_with_weak
from togeari.core.contracts.io import JobRecord, SourceItem
from togeari.core.contracts.telemetry import ADMIT, END
from togeari.core.contracts.views import PoolView
from togeari.core.plugin.base import PluginBlockError
from togeari.core.program.execution import observe_execution, warn_unverified
from togeari.core.runtime.checkpoints import CheckpointResumeError
from togeari.core.runtime.kir import fresh
from togeari.core.runtime.pool.signals import Signals
from togeari.core.runtime.retire import snapshot
from togeari.core.runtime.state import InfState, Job, NameMap, Strand, const, hold
from togeari.core.runtime.values import value_bytes

log = logging.getLogger("togeari.dispatcher")
_DONE = (InfState.DONE, InfState.RETIRED, InfState.SQUASHED)


class JobsStage:
    """Jobs from admission to their record (mixed into ``Dispatcher``)."""

    @staticmethod
    def where(job: Job) -> dict:
        return {"entry": job.entry.name}

    # ------------------------------------------------------------ start
    def start_job(self, item: SourceItem, handle: Any = None) -> None:
        manager = self.m.checkpoints
        original_item = item
        if self.stopping:
            manager.discard_resume(original_item)
            self.reject(item, "Cancelled", "the machine is stopping", handle)
            return
        if manager.prepare_resume(item, handle, lambda prepared: self.start_job(prepared, handle)):
            return
        try:
            item = self.plugins.transform(
                "pre_admit",
                item,
                kind=SourceItem,
                where={"entry": item.entry or self.m.default_entry},
            )
        except PluginBlockError as exc:
            manager.discard_resume(original_item)
            self.reject(item, "Blocked", str(exc), handle)
            return
        if item.key in self.keys:
            manager.discard_resume(original_item)
            self.reject(item, "DuplicateKey", f"job key {item.key!r} is already running", handle)
            return
        try:
            checkpoint = manager.validate_resume(original_item, item)
        except CheckpointResumeError as exc:
            self.reject(
                item,
                "CheckpointIncompatible",
                str(exc),
                handle,
                stats={"checkpoint_restore": {"status": "rejected", "code": exc.code}},
            )
            return
        if checkpoint is not None and not checkpoint["decision"]["allow"]:
            decision = checkpoint["decision"]
            self.reject(
                item,
                "CheckpointIncompatible",
                "; ".join(issue["message"] for issue in decision["issues"]),
                handle,
                stats={"checkpoint_restore": {"status": "rejected", "issues": decision["issues"]}},
            )
            return
        name = item.entry or self.m.default_entry
        entry = self.code.entries.get(name)
        if entry is None:
            self.reject(item, "KirRuntimeError", f"no entry {name!r}", handle)
            return

        # Bind the entry's parameters; the top level takes no inputs
        params = {p.name: p for p in entry.params}
        unknown = sorted(set(item.inputs) - set(params))
        if unknown:
            where = f"@def {name}" if entry.outputs is not None else "the top level"
            msg = f"{where}: unknown inputs {unknown}"
            self.reject(item, "KirRuntimeError", msg, handle, name)
            return
        names = NameMap()
        for p in entry.params:
            if p.name in item.inputs:
                names.set(p.name, const(item.inputs[p.name]))
            elif p.has_default:
                names.set(p.name, const(fresh(p.default)))
            else:
                msg = f"@def {name}: input {p.name!r} missing"
                self.reject(item, "KirRuntimeError", msg, handle, name)
                return

        # A job: one strand at the entry address; sampled for recording and for events
        job = Job(item.key, entry, item.inputs)
        job.start_time = time.perf_counter()
        job.handle = handle
        job.auto_key = item.auto_key
        job.stats["program"] = entry.region.semantic_hash
        observation = observe_execution(entry.region, self.code, self.m)
        job.program_hash = entry.region.semantic_hash
        job.execution_hash = observation["manifest"]["hash"]
        # Observation is a borrowed read-only cache. One detached admission copy
        # can safely escape through this job's eventual output stats.
        pinned_execution = copy.deepcopy(observation["manifest"])
        job.stats["execution"] = pinned_execution
        job.stats["execution_epoch"] = observation["epoch"]
        rate = self.knobs["record"]
        if rate:
            job.record = [] if self.record_rng.random() < rate else None
            job.sampled = job.record is not None
        else:
            job.sampled = self.event_rng.random() < self.knobs["event_sample"]
        job.add_strand(Strand(job, entry.addr, (), 0, (), names))

        # The inputs as given, before any statement runs: positional keys and snapshots
        if self.store is not None:
            dig, weak = digest_with_weak(item.inputs)
            if weak:
                self.note_weak(job, weak)
            else:
                job.inputs_dig = dig

        # Resume from a snapshot of an earlier run of this job
        snap = self.snapshot_key(job) if self.knobs["snapshot_every"] else None
        restored = False
        restore_state = restore_decision = None
        if checkpoint is not None:
            restore_state, restore_decision = checkpoint["state"], checkpoint["decision"]
            warn_unverified(self.m, checkpoint["decision"], execution_hash=job.execution_hash)
            job.stats["execution"] = pinned_execution
            job.stats["execution_epoch"] = observation["epoch"]
            restored = True
        elif snap is not None:
            self.warn_auto_key(job)
            old_key = snapshot.legacy_snapshot_key(
                entry.region.semantic_hash, job.key, job.inputs_dig
            )
            candidate = snapshot.load_candidate(
                self.store,
                snap,
                old_key,
                on_error=lambda e: self.store_error("get", e),
                v2_key=snapshot.v2_snapshot_key(
                    entry.region.semantic_hash, job.key, job.inputs_dig
                ),
            )
            decision = candidate["decision"]
            if candidate["action"] == "candidate":
                prepared = snapshot.prepare_restore(
                    candidate["blob"],
                    entry,
                    self.code,
                    observation["manifest"],
                    self.m.persistence_compatibility,
                )
                decision = prepared["decision"]
                if decision["allow"]:
                    restore_state, restore_decision = prepared["state"], decision
                    warn_unverified(
                        self.m, decision, execution_hash=observation["manifest"]["hash"]
                    )
                    restored = True
            if decision is not None and not decision["allow"]:
                rejection = snapshot.snapshot_rejection(decision)
                self.reject(
                    item,
                    "SnapshotIncompatible",
                    rejection["failure"]["message"],
                    handle,
                    name,
                    stats=rejection["stats"],
                )
                return
            # Local epoch/identity are never inherited from persisted stats.
            job.stats["execution"] = pinned_execution
            job.stats["execution_epoch"] = observation["epoch"]

        requires_events = self.m.event_output is not None and any(
            ins.semantic_emit for ins in self.code.calls(entry.region)
        )
        if restored and restore_state.get("event_state") is not None:
            # Reserve the key while journal frontier I/O is off-dispatcher. Do not
            # re-run pre_admit, apply saved state, or notify/start nodes yet.
            self.keys.add(job.key)

            def finish_frontier(frontier_decision):
                self.keys.discard(job.key)
                if self.stopping:
                    self.reject(item, "Cancelled", "machine stopped during event restore", handle)
                    return
                if not frontier_decision["allow"]:
                    self.reject(
                        item,
                        "EventJournalIncompatible",
                        "; ".join(issue["message"] for issue in frontier_decision["issues"]),
                        handle,
                        name,
                        stats={"event_restore": frontier_decision},
                    )
                    return
                current = observe_execution(entry.region, self.code, self.m)
                if (
                    current["epoch"] != observation["epoch"]
                    or current["manifest"]["hash"] != job.execution_hash
                ):
                    self.reject(
                        item,
                        "SnapshotIncompatible",
                        "execution changed during event frontier validation",
                        handle,
                        name,
                    )
                    return
                snapshot.apply_prepared(job, restore_state, restore_decision)
                self._finish_start_job(
                    job, item, handle, params, True, pinned_execution, observation["epoch"]
                )

            def checked_frontier(frontier_decision):
                try:
                    finish_frontier(frontier_decision)
                except Exception as exc:  # noqa: BLE001
                    # Async callbacks bypass the normal start_job exception guard.
                    # Never leave an unanswered handle or a reserved/half-admitted job.
                    self.keys.discard(job.key)
                    if job.id in self.jobs:
                        self.job_error(job, exc)
                    else:
                        job.freed = None
                        for strand in list(job.strands.values()):
                            job.end_strand(strand)
                        self.stats["machine_errors"] += 1
                        self.reject(
                            item, "MachineError", f"{type(exc).__name__}: {exc}", handle, name
                        )

            if self.m.events is None:
                self.keys.discard(job.key)
                self.reject(
                    item, "EventJournalIncompatible", "event output is unavailable", handle, name
                )
                return
            self.m.events.check_frontier(
                restore_state["job_execution_id"], restore_state["event_state"], checked_frontier
            )
            return
        if restored:
            snapshot.apply_prepared(job, restore_state, restore_decision)
        elif requires_events:
            job.event_state = {
                "version": 1,
                "next_sequence": 0,
                "committed_watermark": -1,
                "journal_identity": copy.deepcopy(self.m.event_output.descriptor()["journal"]),
            }
            job.stats["committed_events"] = {"version": 1, "events": []}
        self._finish_start_job(
            job, item, handle, params, restored, pinned_execution, observation["epoch"]
        )

    def _finish_start_job(self, job, item, handle, params, restored, pinned_execution, epoch):
        """Install a checked admission after optional asynchronous frontier validation."""
        entry = job.entry
        name = entry.name
        job.stats["execution"] = pinned_execution
        job.stats["execution_epoch"] = epoch
        # Region identity is local to this machine, never inherited from a snapshot.
        job.stats["program_region"] = entry.region.index

        # Bytes held: the inputs for the job's life, restored values while their slots live (the
        # entry's parameters are counted once, with the inputs)
        job.freed = self.freed
        job.bytes = value_bytes(item.inputs)
        if restored:
            for st in job.strands.values():
                for n, slot in st.names.to_dict().items():
                    if n not in params:
                        hold(slot, job, value_bytes(slot.value))
        job.stats["bytes_peak"] = max(job.stats.get("bytes_peak", 0), job.bytes)
        self.pool_bytes += job.bytes
        self.jobs[job.id] = job
        self.keys.add(job.key)
        self.dirty[job.id] = None
        self.last_started = job
        self.bus.sample(job.id, job.sampled)
        if self.ring is not None:
            self.ring.put(ADMIT, job.id, entry.addr, None, None, None, None, 0, 0)
        if self.bus.wants("job.admit", job.id):
            self.bus.emit("job.admit", job=job.id, key=job.key, entry=name)
        self.plugins.notify(
            "on_job_start",
            {
                "key": job.key,
                "entry": name,
                "inputs": item.inputs,
                "recorded": job.record is not None,
            },
            where=self.where(job),
        )

    def warn_auto_key(self, job: Job) -> None:
        """Report once that keys are automatic while snapshots or positional results are on."""
        if job.auto_key and not self.warned_keys:
            self.warned_keys = True
            log.warning(
                "job keys are automatic (no key given) while snapshots or positional stores are"
                " on; a job is matched by key and inputs, so give explicit keys"
            )

    def reject(
        self,
        item: SourceItem,
        kind: str,
        msg: str,
        handle: Any = None,
        entry: str | None = None,
        stats: dict | None = None,
    ) -> None:
        rec = JobRecord(
            item.key,
            entry or (item.entry or self.m.default_entry or ""),
            False,
            {},
            {"kind": kind, "node": None, "message": msg},
            stats=stats or {},
        )
        self.stats["jobs_failed"] += 1
        self.plugins.notify("on_job_end", rec, where={"entry": rec.entry})
        self.emit_record(rec, handle)

    # ------------------------------------------------------------ end
    def end_job(self, job: Job, outputs: dict, missing: list) -> None:
        """End a job: its strands end, unfinished work is squashed, its record goes out."""
        job.ended = True
        self.checkpoint_terminal(
            job, "cancelled" if job.failure and job.failure["kind"] == "Cancelled" else "terminal"
        )
        for st in list(job.strands.values()):
            job.end_strand(st)
        for x in job.window.clear():
            if x.state not in _DONE:
                self.squash(x)
        job.held.clear()
        job.memo = None
        for collector in job.collectors.values():
            collector.abort()
        job.collectors.clear()
        self.release_freed()

        # The record
        ok = job.failure is None
        stats = dict(job.stats)
        stats["seconds"] = time.perf_counter() - job.start_time
        stats["steps"] = job.steps
        stats["bytes"] = job.bytes
        if job.event_state is not None:
            stats["event_output"] = self.m.events.status(job.job_execution_id)
        rec = JobRecord(
            job.key, job.entry.name, ok, outputs, job.failure, missing, stats, record=job.record
        )

        # The pool forgets the job
        self.stats["jobs_ok" if ok else "jobs_failed"] += 1
        self.jobs.pop(job.id, None)
        self.keys.discard(job.key)
        self.blocked.pop(job.id, None)
        self.pool_bytes -= job.bytes

        # A finished job's snapshot (taken in this run or resumed from) is gone
        has_snapshot = job.stats.get("snapshots") or job.stats.get("resumed")
        snap = self.snapshot_key(job) if ok and has_snapshot else None
        if snap is not None:
            self.store_delete(snap)

        # Telemetry, event, plugins, then the handle or the sink
        if self.ring is not None:
            match job.failure:
                case None:
                    status = 0
                case {"kind": "Cancelled"}:
                    status = 2
                case _:
                    status = 1
            self.ring.put(
                END, job.id, job.entry.addr, None, None, None, stats["seconds"], 0, status
            )
        if self.bus.wants("job.end", job.id):
            self.bus.emit(
                "job.end",
                job=job.id,
                key=job.key,
                ok=ok,
                entry=job.entry.name,
                failure=job.failure,
                stats=stats,
            )
        self.bus.forget_job(job.id)
        self.plugins.notify("on_job_end", rec, where=self.where(job))
        self.emit_record(rec, job.handle)

    def job_error(self, job: Job, exc: BaseException) -> None:
        """A failure inside the machine while working on one job: that job fails, the rest go on."""
        log.error("job %s failed inside the machine", job.key, exc_info=exc)
        self.stats["machine_errors"] += 1
        if job.ended or job.id not in self.jobs:
            return
        job.failure = {
            "kind": "MachineError",
            "node": None,
            "message": f"{type(exc).__name__}: {exc}",
        }
        self.end_job(job, {}, list(job.entry.outputs or []))

    def expire_event_commits(self) -> list[dict]:
        """Bounded shutdown escape: unresolved append is unknown, never asserted absent."""
        unknown = self.m.events.force_unknown()
        affected = {entry["job_execution_id"] for entry in unknown}
        for job in list(self.jobs.values()):
            if job.job_execution_id in affected:
                job.event_cancel = None
                job.failure = {
                    "kind": "EventCommitUnknown",
                    "node": None,
                    "message": "event journal append did not acknowledge before shutdown deadline",
                }
                job.stats["event_commit_unknown"] = [
                    entry["event_id"]
                    for entry in unknown
                    if entry["job_execution_id"] == job.job_execution_id
                ]
                self.end_job(job, {}, list(job.entry.outputs or []))
            elif job.event_cancel is not None or any(
                inf.instr.semantic_emit and inf.event_commit is not None for inf in job.window
            ):
                message, job.event_cancel = job.event_cancel or "machine event output stopped", None
                self.cancel_job(job, message)
        return unknown

    def cancel_job(self, job: Job, message: str) -> None:
        # An append may already be durable while its ACK is in flight. Finish
        # retiring that exact head before terminal cancellation, never squash it.
        if any(
            inf.instr.semantic_emit
            and inf.event_commit is not None
            and inf.event_commit["status"] in ("pending", "committed")
            for inf in job.window
        ):
            job.event_cancel = message
            self.dirty[job.id] = None
            return
        job.failure = {"kind": "Cancelled", "node": None, "message": message}
        self.end_job(job, {}, list(job.entry.outputs or []))

    def pause_job(self, job: Job) -> bool:
        """Hold the job's ready statements and stop fetching for it."""
        job.paused = True
        return True

    def resume_job(self, job: Job) -> bool:
        """Release the held statements and fetch again."""
        job.paused = False
        self.ready.extend(job.held)
        job.held.clear()
        self.dirty[job.id] = None
        return True

    def job_count(self) -> int:
        return len(self.jobs)

    def window_total(self) -> int:
        """Statements in the windows of every resident job."""
        return sum(len(j.window) for j in self.jobs.values())

    def inline_totals(self) -> dict[str, float]:
        """Seconds spent in each inline node."""
        return {k: st.total for k, st in self.inline.items()}

    # ------------------------------------------------------------ records
    def emit_record(self, rec: JobRecord, handle: Any = None) -> None:
        try:
            out = self.plugins.transform(
                "pre_sink", rec, kind=JobRecord, where={"entry": rec.entry}
            )
        except PluginBlockError as exc:
            self.stats["records_blocked"] += 1
            if handle is not None:
                rec.stats["sink_blocked"] = str(exc)
                handle._set(rec)
            return
        if handle is not None:
            handle._set(out)
        elif self.sink_q is not None:
            self.to_sink(out)
        else:
            self.stats["records_dropped"] += 1

    def to_sink(self, rec: JobRecord) -> None:
        """Hand a record to the sink writer; a full queue holds it here (and pauses admission)."""
        if not self.sink_pending:
            try:
                self.sink_q.put_nowait(rec)
                return
            except queue.Full:
                pass
        self.sink_pending.append(rec)
        self.flush_sink()

    def flush_sink(self) -> None:
        while self.sink_pending:
            try:
                self.sink_q.put_nowait(self.sink_pending[0])
            except queue.Full:
                # Full: the writer wakes the dispatcher after its next take; one more try
                self.sink_wait = True
                try:
                    self.sink_q.put_nowait(self.sink_pending[0])
                except queue.Full:
                    return
            self.sink_pending.popleft()

    # ------------------------------------------------------------ admission
    def release_freed(self) -> None:
        """Apply the bytes released by freed held slots to their jobs and the pool."""
        freed = self.freed
        while freed:
            job, size = freed.popleft()
            if not job.ended:
                job.bytes -= size
                self.pool_bytes -= size

    def admit(self) -> None:
        if self.source_buf is None or self.stopping or self.sink_pending:
            return
        rule = self.knobs["admission"]
        while True:
            self.release_freed()
            sig = Signals(self)
            if self.jobs:
                pool = PoolView(len(self.jobs), self.pool_bytes)
                try:
                    allowed = rule.allows(pool, sig)
                except Exception as exc:  # noqa: BLE001
                    self.policy_failed("admission", exc)
                    return
                if not (allowed and self.plugins.vote("vote_admit", sig)):
                    return
            item = self.take_source()
            if item is None:
                return
            self.last_started = None
            try:
                self.start_job(item)
            except Exception as exc:
                log.error("job %s could not start", item.key, exc_info=exc)
                self.stats["machine_errors"] += 1
                self.reject(item, "MachineError", f"{type(exc).__name__}: {exc}")
                continue

            # The new job fetches and issues at once: the next check counts its load
            job = self.last_started
            if job is not None:
                try:
                    self.front.advance(job)
                except Exception as exc:  # noqa: BLE001
                    self.job_error(job, exc)
                self.issue()

    def take_source(self) -> SourceItem | None:
        """The next buffered source item, or None; after the last one the buffer is let go."""
        buf = self.source_buf
        try:
            return buf.get_nowait()
        except queue.Empty:
            pass

        # The reader wakes the dispatcher after its next put, or after its last
        self.source_want = True
        finished = self.run is not None and self.run.source_finished
        try:
            return buf.get_nowait()
        except queue.Empty:
            if finished:
                self.source_buf = None
            return None

    def signals(self) -> dict:
        return Signals(self).to_dict()
