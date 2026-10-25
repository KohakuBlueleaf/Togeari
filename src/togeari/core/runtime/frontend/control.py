"""Branches without guessing: park, continuation strands, phis and joins (spec/03 §3.4)."""

from togeari.core.contracts.telemetry import PARK
from togeari.core.program.decode.instr import ALL
from togeari.core.runtime.kir import UNDEF, RuntimeFault, undefined
from togeari.core.runtime.state import (
    Frame,
    Join,
    NameMap,
    Names,
    Slot,
    SlotState,
    Strand,
    StrandState,
    effective,
)


class ControlOps:
    """Control instructions of the front end (mixed into ``FrontEnd``)."""

    # ------------------------------------------------------------ joins
    @staticmethod
    def phi_names(names: Names | None, writes: frozenset) -> list[str]:
        """The names a join needs phis for, sorted: the region's writes, or every name with
        ``ALL``."""
        if ALL in writes:
            return sorted(set(names.keys() if names is not None else ()) | (writes - {ALL}))
        return sorted(writes)

    def make_join(self, names: Names | None, writes: frozenset, table) -> Join:
        phis = {n: Slot(phi=True) for n in self.phi_names(names, writes)}
        return Join(phis, table, ALL in writes)

    def resolve_join(self, join: Join, names: NameMap) -> None:
        for n, phi in join.phis.items():
            if phi.forward is None and phi.phi:
                self.d.resolve_phi(phi, names.get(n) or undefined())
        join.resolved = True

    def early_resolve(self, st: Strand) -> None:
        """Resolve a phi as soon as no remaining path of the region can write its name."""
        f = next((f for f in reversed(st.frames) if f.join is not None), None)
        if f is None or f.join.resolved or f.join.table is None:
            return
        w = f.join.table.get(st.pc, frozenset()) if st.pc != f.reconv else frozenset()
        if ALL in w:
            return

        done = True
        for n, phi in f.join.phis.items():
            if phi.forward is not None:
                continue
            if n in w:
                done = False
                continue
            self.d.resolve_phi(phi, st.names.get(n) or undefined())
        f.join.resolved = done

    # ------------------------------------------------------------ loops
    @staticmethod
    def own_frame(st: Strand, addr: int) -> int | None:
        """The index of the open frame the control instruction at ``addr`` opened, when the
        strand has come back to it (a loop); only unparked branch frames may lie above it."""
        for i in range(len(st.frames) - 1, -1, -1):
            f = st.frames[i]
            if f.kind != "branch":
                return None
            if f.branch == addr:
                return i
            if f.join is not None:
                return None
        return None

    @staticmethod
    def reenter(st: Strand, i: int) -> None:
        """Back at the branch of frame ``i``: the frames above it close, and the strand keeps
        numbering inside frame ``i``'s arm after every key those frames used."""
        if i + 1 < len(st.frames):
            st.prefix, st.counter = st.frames[i + 1].saved
            st.frames = st.frames[: i + 1]

    # ------------------------------------------------------------ deciding
    def control_value(self, st: Strand, ins):
        """The ready value of the operand as a 1-tuple, or None after parking / stalling."""
        slot = self.slot_of(st, ins.operand)
        s = effective(slot, st.next_key())
        if s.state == SlotState.READY:
            if s.value is UNDEF:
                raise RuntimeFault(f"{ins.operand[1]!r} is not defined")
            return (s.value,)

        # Pending: run ahead past a structured region, or wait here; a loop back at its own
        # branch waits inside the region it already holds
        tables = self.code.tables(ins.addr)
        can_park = (
            st.resume != "parked"
            and self.knobs["run_ahead"]
            and tables.structured(ins.addr)
            and not st.job.paused
            and st.job.cut is None
            and self.own_frame(st, ins.addr) is None
        )
        if can_park:
            self.park(st, ins, s)
        elif st.resume == "parked":
            st.state = StrandState.PARKED
            s.waiters.append(st)
        else:
            st.state = StrandState.STALLED
            st.resume = "stalled"
            s.waiters.append(st)
        return None

    def park(self, st: Strand, ins, waiting_on: Slot) -> None:
        tables = self.code.tables(ins.addr)
        reconv = tables.reconv[ins.addr]
        key = self.consume(st)
        job = st.job

        # A continuation strand at the reconvergence point reads phis for the region's writes
        names_now = st.names.snapshot()
        join = self.make_join(names_now, tables.may_write(ins.addr), tables.region_writes(ins.addr))
        c = Strand(job, reconv, st.prefix, st.counter, st.frames, NameMap.of(names_now, join.phis))
        job.add_strand(c)

        # The parked strand keeps the region: its join frame and the arm's key prefix
        st.frames = (*st.frames, Frame(reconv, (st.prefix, st.counter), join, ins.addr))
        st.prefix, st.counter = (*key, 1), 0
        st.state = StrandState.PARKED
        st.resume = "parked"
        job.stats["parks"] += 1
        waiting_on.waiters.append(st)
        if self.d.ring is not None:
            self.d.ring.put(PARK, job.id, ins.addr, None, None, None, None, st.id, 0)
        if self.d.bus.wants("strand.park", job.id):
            self.d.bus.emit("strand.park", job=job.id, strand=st.id, addr=ins.addr, cont=c.id)

    def enter_region(self, st: Strand, ins, target: int) -> None:
        """Take a decided branch: open its key frame (structured) and jump."""
        if st.resume == "parked":
            st.resume = False
            st.pc = target
            return

        # A loop back at its own branch: the branch takes its key inside the frame it opened
        st.resume = False
        i = self.own_frame(st, ins.addr)
        if i is not None:
            self.reenter(st, i)
            self.consume(st)
            st.pc = target
            return

        # Not parked: the branch takes its key now and opens a frame if structured
        tables = self.code.tables(ins.addr)
        key = self.consume(st)
        if tables.structured(ins.addr):
            st.frames = (
                *st.frames,
                Frame(tables.reconv[ins.addr], (st.prefix, st.counter), None, ins.addr),
            )
            st.prefix, st.counter = (*key, 1), 0
        st.pc = target

    # ------------------------------------------------------------ instructions
    def op_branch(self, st: Strand, ins) -> None:
        got = self.control_value(st, ins)
        if got is None:
            return
        match got[0]:
            case bool() as cond:
                self.enter_region(st, ins, ins.targets[0] if cond else ins.targets[1])
            case other:
                raise RuntimeFault(f"branch condition is {type(other).__name__}, not bool")

    def op_switch(self, st: Strand, ins) -> None:
        got = self.control_value(st, ins)
        if got is None:
            return
        v = got[0]
        target = next((t for case, t in ins.cases if case == v), ins.default)
        if target is None:
            raise RuntimeFault(f"switch: no case matches {v!r}")
        self.enter_region(st, ins, target)
