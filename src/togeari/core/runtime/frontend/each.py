"""`each`: iterations at once (lazily started strands) or in order on one strand (spec/03 §3.5)."""

from typing import Any

from togeari.core.program.decode.instr import ALL
from togeari.core.runtime.kir import RuntimeFault
from togeari.core.runtime.state import Frame, Join, NameMap, Names, Slot, Strand, const


class EachSpawn:
    """The iterations of one `each` still to start, and what each new one reads."""

    __slots__ = (
        "ins",
        "items",
        "key",
        "base",
        "saved",
        "frames",
        "carried",
        "table",
        "last",
        "prev",
    )

    def __init__(self, ins, items, key, base, saved, frames, carried, table, last):
        self.ins = ins
        self.items = items
        self.key = key
        self.base: Names = base
        self.saved = saved
        self.frames = frames
        # Names each iteration's join carries to the next, sorted
        self.carried: list[str] = carried
        self.table = table
        # The last iteration's join (the names after the loop read its phis)
        self.last: Join = last
        self.prev: Join | None = None


class EachOps:
    """`each` instructions of the front end (mixed into ``FrontEnd``)."""

    def bind_item(self, st: Strand, each, items: list, i: int) -> None:
        if each.item is not None:
            st.names.set(each.item, const(items[i]))
        if each.index is not None:
            st.names.set(each.index, const(i))

    def op_each(self, st: Strand, ins) -> None:
        got = self.control_value(st, ins)
        if got is None:
            return
        try:
            items = list(got[0])
        except TypeError as exc:
            raise RuntimeFault(f"each: items are not iterable ({exc})") from None

        # The `each`'s own key (taken when it parked, or now)
        if st.resume == "parked":
            saved_prefix, saved_counter = st.frames[-1].saved
            key = (*saved_prefix, saved_counter - 1)
        else:
            key = self.consume(st)
        st.resume = False

        if not items:
            st.pc = ins.after
        elif ins.has_exit:
            self.each_in_order(st, ins, items, key)
        else:
            self.each_at_once(st, ins, items, key)

    # ------------------------------------------------------------ in order
    def each_in_order(self, st: Strand, ins, items: list, key: tuple) -> None:
        """A body that can leave the loop: iterations follow the strand's program counter."""
        reconv = self.code.tables(ins.addr).reconv.get(ins.addr)
        f = Frame(reconv, (st.prefix, st.counter), None, ins.addr, "iter", ins.addr, items, 0, key)
        st.frames = (*st.frames, f)
        st.prefix, st.counter = (*key, 0), 0
        self.bind_item(st, ins, items, 0)
        st.pc = ins.body

    def op_iter_end(self, st: Strand, ins) -> None:
        f = st.frames[-1] if st.frames else None
        if f is None or f.kind != "iter" or f.each != ins.each_addr:
            raise RuntimeFault("internal: iteration frame missing")
        self.consume(st)
        each = self.mem[f.each]

        # Next iteration, or leave the loop
        i = f.i + 1
        if i < len(f.items):
            st.frames = (*st.frames[:-1], f.replace(i=i))
            st.prefix, st.counter = (*f.base, i), 0
            self.bind_item(st, each, f.items, i)
            st.pc = each.body
        else:
            st.frames = st.frames[:-1]
            st.prefix, st.counter = f.saved
            st.pc = each.after

    # ------------------------------------------------------------ at once
    def each_at_once(self, st: Strand, ins, items: list, key: tuple) -> None:
        """One strand per item, started lazily in index order by a spawner strand.

        Iteration i+1 reads the carried names of iteration i through phis; the
        strand itself continues after the loop with the last iteration's phis.
        """
        tables = self.code.tables(ins.addr)
        writes = tables.body_may_write(ins.addr)
        table = tables.body_writes(ins.addr)
        base = st.names.snapshot()
        job = st.job

        # The last iteration's join covers every body write (the names after the loop); phis
        # are made in name order
        extra = {n for n in (ins.item, ins.index) if n is not None}
        if ALL in writes:
            last_names = sorted(set(base.keys()) | (writes - {ALL}) | extra)
            carried = last_names
        else:
            last_names = sorted(writes)
            carried = sorted(tables.carried(ins.addr))
        last = Join({n: _phi() for n in last_names}, table, ALL in writes)

        # A spawner starts the iterations as the window allows
        e = EachSpawn(
            ins, items, key, base, (st.prefix, st.counter), st.frames, carried, table, last
        )
        job.add_strand(Strand(job, ins.addr, key, 0, st.frames, None, spawn=e))

        # The strand itself continues after the loop
        after = dict(last.phis)
        if ins.item is not None:
            after[ins.item] = const(items[-1])
        if ins.index is not None:
            after[ins.index] = const(len(items) - 1)
        st.names = NameMap.of(base, after)
        st.pc = ins.after

    def spawn_next(self, sp: Strand) -> int:
        """Start the next iteration strand; returns 1 (one step)."""
        e: EachSpawn = sp.spawn
        ins, items, job = e.ins, e.items, sp.job
        i = sp.counter

        # Names: those at the `each`, the previous iteration's carried phis, the item and index
        extra: dict[str, Any] = dict(e.prev.phis) if e.prev is not None else {}
        if ins.item is not None:
            extra[ins.item] = const(items[i])
        if ins.index is not None:
            extra[ins.index] = const(i)
        if i == len(items) - 1:
            join = e.last
        else:
            join = Join({n: _phi() for n in e.carried}, e.table, e.last.all_names)

        # The iteration strand, ending at ITER_END with its join resolved
        f = Frame(ins.iter_end, e.saved, join, ins.addr, "iter", ins.addr, items, i, e.key)
        it = Strand(job, ins.body, (*e.key, i), 0, (*e.frames, f), NameMap.of(e.base, extra))
        job.add_strand(it)
        e.prev = join
        sp.counter = i + 1
        if sp.counter == len(items):
            self.end(sp)
        return 1


def _phi() -> Slot:
    return Slot(phi=True)
