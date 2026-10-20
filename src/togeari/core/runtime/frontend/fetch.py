"""Fetch and rename: strands walk code memory in program order (spec/03 §3.3, §3.6).

Every instruction a strand fetches takes the next order key, so keys never
depend on timing. A job's runnable strands run oldest key first; a strand
stops at the window caps unless it holds the job's oldest unfetched statement.
"""

import heapq
from typing import Any

from togeari.core.program.decode.instr import Op
from togeari.core.runtime.frontend.control import ControlOps
from togeari.core.runtime.frontend.each import EachOps
from togeari.core.runtime.kir import RuntimeFault, fresh
from togeari.core.runtime.state import (
    Inflight,
    Slot,
    Strand,
    StrandState,
    const,
    deref,
)

# Instructions one front-end pass may run for a job before the job yields to others
FETCH_BUDGET = 20_000


def snapshot_of(st: Strand) -> tuple:
    """A strand's view just before its next statement (fault handling restores it)."""
    return (st.names.snapshot(), st.frames, st.prefix, st.counter)


class FrontEnd(ControlOps, EachOps):
    def __init__(self, d: Any):
        self.d = d
        self.code = d.code
        self.mem = d.code.mem
        self.knobs = d.knobs
        self.ops = {
            Op.CALL: self.op_call,
            Op.ASSIGN: self.op_assign,
            Op.BRANCH: self.op_branch,
            Op.SWITCH: self.op_switch,
            Op.JUMP: self.op_jump,
            Op.EACH: self.op_each,
            Op.ITER_END: self.op_iter_end,
            Op.END: self.op_end,
        }

    # ------------------------------------------------------------ driving
    def advance(self, job) -> None:
        """Run the job's runnable strands, oldest key first, until each stops or the budget ends."""
        budget = FETCH_BUDGET
        heap = [(s.next_key(), s.id, s) for s in job.take_runnable()]
        heapq.heapify(heap)
        capped: list = []

        while heap and budget > 0 and not job.ended:
            _, _, st = heapq.heappop(heap)
            if st.state != StrandState.RUNNABLE:
                continue
            budget -= self.run(st, budget)

            # Stopped at a cap: wait there; stopped for the budget or a yield: run again
            if st.state == StrandState.RUNNABLE:
                if self.blocked_by_caps(st):
                    heapq.heappush(capped, (st.next_key(), st.id, st))
                else:
                    heapq.heappush(heap, (st.next_key(), st.id, st))

            # Strands made runnable meanwhile, and a capped strand that is now the oldest
            for s in job.take_runnable():
                heapq.heappush(heap, (s.next_key(), s.id, s))
            while capped and capped[0][2].state != StrandState.RUNNABLE:
                heapq.heappop(capped)
            if capped and not self.blocked_by_caps(capped[0][2]):
                heapq.heappush(heap, heapq.heappop(capped))

        # Whatever did not run stays runnable; a budget stop runs again next cycle
        for _, _, s in heap + capped:
            if s.state == StrandState.RUNNABLE:
                job.mark_runnable(s)
        if heap and not job.ended:
            self.d.dirty[job.id] = None

    def blocked_by_caps(self, st: Strand) -> bool:
        job = st.job
        k = self.knobs
        if job.unfinished < k["window"] and len(job.window) - job.unfinished < k["retire_buffer"]:
            return False

        # Progress rule: the strand holding the oldest unfetched statement fetches
        key = st.next_key()
        if key != job.oldest_live_key():
            return True

        # A spawner waits while older unfinished work will free the window
        if st.spawn is not None:
            older = job.oldest_open_key()
            return older is not None and older < key
        return False

    def run(self, st: Strand, budget: int) -> int:
        """Fetch from one strand until it stops; returns the instructions it ran."""
        if st.spawn is not None:
            return 0 if self.blocked_by_caps(st) else self.spawn_next(st)
        job = st.job
        limit = self.knobs["step_budget"]
        n = 0
        while st.state == StrandState.RUNNABLE and n < budget:
            if self.blocked_by_caps(st):
                break
            if not self.pop_frames(st):
                n += 1
                break

            # One instruction; KIR runtime errors become uncatchable faults
            ins = self.mem[st.pc]
            n += 1
            job.steps += 1
            try:
                if limit is not None and job.steps > limit:
                    raise RuntimeFault(f"more than {limit} steps")
                self.ops[ins.op](st, ins)
            except RuntimeFault as exc:
                self.control_fault(st, ins, exc)
                break

            if st.state == StrandState.RUNNABLE:
                self.early_resolve(st)
        return n

    def pop_frames(self, st: Strand) -> bool:
        """Leave every region whose reconvergence point is ``st.pc``; False if the strand ended."""
        while st.frames and st.frames[-1].reconv == st.pc:
            f = st.frames[-1]
            st.frames = st.frames[:-1]
            if f.join is not None:
                self.resolve_join(f.join, st.names)
                self.end(st)
                return False
            if f.saved is not None:
                st.prefix, st.counter = f.saved
        return True

    @staticmethod
    def consume(st: Strand) -> tuple:
        k = (*st.prefix, st.counter)
        st.counter += 1
        return k

    def end(self, st: Strand) -> None:
        st.job.end_strand(st)
        if self.d.bus.wants("strand.end", st.job.id):
            self.d.bus.emit("strand.end", job=st.job.id, strand=st.id, addr=st.pc)

    # ------------------------------------------------------------ operands
    def slot_of(self, st: Strand, op) -> Slot:
        match op:
            case ("n", name):
                s = st.names.get(name)
                if s is None:
                    raise RuntimeFault(f"{name!r} is not defined")
                return s
            case ("l" | "d", value):
                return const(fresh(value))
            case (_, port):
                raise RuntimeFault(f"input {port!r} missing")

    # ------------------------------------------------------------ faults
    def control_fault(self, st: Strand, ins, exc: Exception) -> None:
        key = self.consume(st)
        inf = Inflight(st.job, ins, key)
        inf.control = True
        inf.snapshot = snapshot_of(st)
        self.end(st)
        st.job.add(inf)
        self.d.fault(inf, exc, catchable=False)

    # ------------------------------------------------------------ simple instructions
    def op_call(self, st: Strand, ins) -> None:
        key = self.consume(st)
        inf = Inflight(st.job, ins, key)

        # Inputs: the slot each name holds now, or literals
        for port, op in ins.args:
            inf.inputs.append((port, self.slot_of(st, op)))
        inf.snapshot = snapshot_of(st)

        # `mutates` ports: a new version of the value, visible to younger readers
        if ins.mutates:
            ports = dict(inf.inputs)
            inf.versions = {}
            for port, _ in ins.mutates:
                before = ports[port]
                version = Slot()
                deref(before).muts.append((key, version))
                inf.versions[port] = (before, version)

        # Outputs: fresh slots, renamed into the strand's names
        for o in ins.outs:
            if o is None:
                inf.outs.append(None)
                continue
            s = Slot()
            inf.outs.append(s)
            st.names.set(o, s)

        st.pc = ins.next
        self.d.admit_inflight(inf)

    def op_assign(self, st: Strand, ins) -> None:
        self.consume(st)
        st.names.set(ins.target, self.slot_of(st, ins.operand))
        st.pc = ins.next

    def op_jump(self, st: Strand, ins) -> None:
        self.consume(st)
        st.pc = ins.targets[0]

    def op_end(self, st: Strand, ins) -> None:
        key = self.consume(st)
        st.job.final_names = st.names.snapshot()
        st.job.final_key = key
        self.end(st)
