"""Conservative requested cuts: drain issued work, rewind only a never-issued suffix.

This stage owns scheduling gates and continuation reconstruction, never store I/O.
The checkpoint service owns tokens, persistence acknowledgements and request replies.
"""

import math
from dataclasses import dataclass, field

from togeari.core.program.decode.instr import Op
from togeari.core.program.execution import compatibility, execution_for_job
from togeari.core.runtime.retire.codec import encode_snapshot
from togeari.core.runtime.state import InfState, NameMap, SlotState, Strand, StrandState, effective


@dataclass
class CutState:
    request_id: str
    deadline: float | None
    step_budget: int | None
    frontier: tuple | None
    steps: int = 0
    phase: str = "draining"
    held: list = field(default_factory=list)


@dataclass
class _Candidate:
    key: tuple
    pc: int
    names: object
    frames: tuple
    source: object = None


class CheckpointStage:
    def begin_checkpoint(self, job_id, request_id, deadline, step_budget) -> dict:
        """Request a cut at a dispatcher safe point; no persistence occurs here."""
        job = self.jobs.get(job_id)
        if job is None or job.ended:
            return {"status": "not_found"}
        if job.cut is not None:
            return {"status": "busy", "request_id": job.cut.request_id}
        if type(request_id) is not str or not request_id:
            raise ValueError("checkpoint request_id must be a nonempty string")
        if deadline is not None and (
            type(deadline) not in (int, float) or not math.isfinite(deadline)
        ):
            raise ValueError("checkpoint deadline must be finite or None")
        if step_budget is not None and (type(step_budget) is not int or step_budget < 0):
            raise ValueError("checkpoint step_budget must be a nonnegative integer or None")
        if job.inputs_dig is None:
            return {
                "status": "weak_inputs",
                "message": "checkpoint needs original strong input identity",
            }
        job.cut = CutState(request_id, deadline, step_budget, job.checkpoint_watermark)
        self._checkpoint_frontier(job)
        if job.paused and (job.window or job.collectors or len(job.strands) != 1):
            job.cut = None
            return {"status": "needs_resume"}
        self.checkpoint_requests[request_id] = job
        self.dirty[job.id] = None
        self.progress_checkpoint(job)
        if job.paused and job.cut is not None:
            self._finish_checkpoint(job, "needs_resume", {})
        return {"status": "pending", "request_id": request_id}

    def cancel_checkpoint(self, request_id) -> dict:
        job = self.checkpoint_requests.get(request_id)
        if job is not None and job.cut is not None:
            self._finish_checkpoint(job, "cancelled", {})
            return {"status": "cancelled", "request_id": request_id}
        return {"status": "not_found", "request_id": request_id}

    def _checkpoint_frontier(self, job) -> None:
        cut = job.cut
        keys = [cut.frontier] if cut.frontier is not None else []
        if job.checkpoint_watermark is not None:
            keys.append(job.checkpoint_watermark)
        keys.extend(x.key for x in job.window if x.state in (InfState.ISSUED, InfState.DONE))
        keys.extend((*collector.key, collector.count) for collector in job.collectors.values())
        cut.frontier = max(keys) if keys else None

    def checkpoint_fetch_allowed(self, strand) -> bool:
        cut = strand.job.cut
        if cut is None:
            return True
        self._checkpoint_frontier(strand.job)
        return cut.frontier is not None and strand.next_key() <= cut.frontier

    def checkpoint_issue_allowed(self, inf) -> bool:
        cut = inf.job.cut
        if cut is None:
            return True
        self._checkpoint_frontier(inf.job)
        if cut.frontier is not None and inf.key <= cut.frontier:
            return True
        if inf not in cut.held:
            cut.held.append(inf)
        return False

    def checkpoint_step(self, job) -> bool:
        cut = job.cut
        if cut is None:
            return True
        if cut.deadline is not None and self.clock() >= cut.deadline:
            self._finish_checkpoint(job, "timeout", {})
            return False
        if cut.step_budget is not None and cut.steps >= cut.step_budget:
            self._finish_checkpoint(job, "budget_exhausted", {"steps": cut.steps})
            return False
        cut.steps += 1
        return True

    def checkpoint_fault(self, job, key) -> None:
        """Normal faults win; diagnose before any protected outcome is discarded."""
        if job.cut is None:
            return
        invalidated = [
            x.key
            for x in job.window
            if x.key > key
            and (
                x.attempt_ids
                or (
                    x.state == InfState.DONE
                    and not x.control
                    and x.fault is None
                    and x.resolution in ("content_hit", "positional_hit", "squash_reuse")
                )
            )
        ]
        if invalidated:
            self._finish_checkpoint(
                job, "fault_interrupted", {"fault_key": key, "invalidated": invalidated}
            )

    def checkpoint_deadline(self):
        deadlines = [
            job.cut.deadline
            for job in self.checkpoint_requests.values()
            if job.cut is not None and job.cut.deadline is not None
        ]
        return min(deadlines) if deadlines else None

    def checkpoint_terminal(self, job, status="terminal") -> None:
        if job.cut is not None:
            self._finish_checkpoint(job, status, {})

    def _release_checkpoint(self, job):
        cut, job.cut = job.cut, None
        if cut is None:
            return None
        self.checkpoint_requests.pop(cut.request_id, None)
        self.ready.extend(x for x in cut.held if x.state == InfState.READY)
        if not job.ended and not job.paused:
            for strand in job.strands.values():
                if strand.state == StrandState.RUNNABLE:
                    job.mark_runnable(strand)
            self.dirty[job.id] = None
        return cut

    def _finish_checkpoint(self, job, status, details) -> None:
        cut = self._release_checkpoint(job)
        if cut is not None:
            self.m.checkpoints.finished(cut.request_id, status, details)

    def _checkpoint_candidates(self, job) -> list[_Candidate]:
        candidates = []
        for x in job.window:
            if x.state not in (InfState.WAITING, InfState.READY) or x.snapshot is None:
                continue
            names, frames, _, _ = x.snapshot
            candidates.append(_Candidate(x.key, x.instr.addr, names, frames, x))
        for strand in job.strands.values():
            key, pc, frames = strand.next_key(), strand.pc, strand.frames
            if strand.resume == "parked" and frames and frames[-1].join is not None:
                top = frames[-1]
                key = (*top.saved[0], top.saved[1] - 1)
                pc, frames = top.branch, frames[:-1]
            candidates.append(_Candidate(key, pc, strand.names, frames, strand))
        if not job.strands and job.final_names is not None and job.final_key is not None:
            region = job.entry.region
            end = next(
                ins.addr for ins in self.code.mem[region.start : region.end] if ins.op == Op.END
            )
            candidates.append(_Candidate(job.final_key, end, job.final_names, ()))
        return sorted(candidates, key=lambda candidate: candidate.key)

    def progress_checkpoint(self, job) -> None:
        cut = job.cut
        if cut is None:
            return
        if cut.deadline is not None and self.clock() >= cut.deadline:
            self._finish_checkpoint(job, "timeout", {})
            return
        self._checkpoint_frontier(job)
        if job.collectors:
            self._checkpoint_budget(job)
            return
        if any(
            x.state in (InfState.ISSUED, InfState.DONE)
            or (cut.frontier is not None and x.key <= cut.frontier)
            for x in job.window
        ):
            self._checkpoint_budget(job)
            return
        candidates = self._checkpoint_candidates(job)
        if not candidates:
            self._finish_checkpoint(job, "terminal", {})
            return
        candidate = candidates[0]
        if cut.frontier is not None and candidate.key <= cut.frontier:
            self._checkpoint_budget(job)
            return
        if isinstance(candidate.source, Strand) and candidate.source.spawn is not None:
            if job.paused:
                return
            if self.checkpoint_step(job):
                self.front.spawn_next(candidate.source)
                self.progress_checkpoint(job)
            return
        if candidate.names is None:
            self._checkpoint_budget(job)
            return
        values, slots = {}, {}
        for name, slot in candidate.names.to_dict().items():
            slot = effective(slot, candidate.key)
            if slot.state != SlotState.READY:
                self._checkpoint_budget(job)
                return
            values[name] = slot.value
            slots[name] = slot
        frames = tuple(
            (
                frame.replace(join=None, reconv=self.code.tables(frame.each).reconv.get(frame.each))
                if frame.kind == "iter"
                else frame.replace(join=None)
            )
            for frame in candidate.frames
        )
        state = {
            "job_execution_id": job.job_execution_id,
            "pc": candidate.pc,
            "prefix": candidate.key[:-1],
            "counter": candidate.key[-1],
            "frames": [
                {
                    "reconv": frame.reconv,
                    "saved": frame.saved,
                    "branch": frame.branch,
                    "kind": frame.kind,
                    "each": frame.each,
                    "items": frame.items,
                    "i": frame.i,
                    "base": frame.base,
                    "joined": False,
                }
                for frame in frames
            ],
            "values": values,
            "stats": dict(job.stats),
            "steps": job.steps,
            "record": job.record,
        }
        cut.phase = "cutting"
        try:
            manifest = execution_for_job(job, self.code, self.m)
            decision = compatibility(manifest, manifest, self.m.persistence_compatibility)
            if not decision["allow"]:
                self._finish_checkpoint(job, "incompatible", {"decision": decision})
                return
            blob = encode_snapshot(state, job.entry, self.code, manifest)
            names = NameMap(slots)
            continuation = Strand(
                job, candidate.pc, candidate.key[:-1], candidate.key[-1], frames, names
            )
        except Exception as exc:  # noqa: BLE001
            self._finish_checkpoint(job, "failed", {"message": f"{type(exc).__name__}: {exc}"})
            return
        # Pickle hooks may take arbitrary time. Never destructively collapse after
        # the request's deadline, even if encoding began while it was still valid.
        if cut.deadline is not None and self.clock() >= cut.deadline:
            self._finish_checkpoint(job, "timeout", {})
            return
        for x in job.window.clear():
            self.squash(x)
        for strand in list(job.strands.values()):
            job.end_strand(strand)
        job.runnable.clear()
        job.sheap.clear()
        job.wheap.clear()
        job.final_names = job.final_key = None
        job.held.clear()
        self.blocked.pop(job.id, None)
        self.ready = [x for x in self.ready if x.job is not job]
        job.add_strand(continuation)
        info = {
            "key": candidate.key,
            "pc": candidate.pc - job.entry.region.start,
            "frontier": cut.frontier,
            "steps": cut.steps,
            "retired": job.stats["retired"],
            "job_execution_id": job.job_execution_id,
            "inputs_digest": job.inputs_dig,
            "job_key": job.key,
            "entry": job.entry.name,
            "program_hash": job.program_hash,
        }
        request_id = cut.request_id
        self._release_checkpoint(job)
        self.m.checkpoints.captured(request_id, blob, info)

    def _checkpoint_budget(self, job) -> None:
        cut = job.cut
        if cut is not None and cut.step_budget is not None and cut.steps >= cut.step_budget:
            self._finish_checkpoint(job, "budget_exhausted", {"steps": cut.steps})
