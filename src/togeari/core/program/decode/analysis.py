"""Decode tables from the flow graph: reconvergence points and may-write sets."""

from togeari.core.program.decode.instr import ALL, Instr, Op

EXIT = -1


def ipdoms(mem: list[Instr], addrs: list[int]) -> dict[int, int | None]:
    """Immediate post-dominator of every address (Cooper-Harvey-Kennedy on the reverse graph).

    Over flow edges only; fault paths have no edges.
    """
    preds_rev: dict[int, list[int]] = {a: [] for a in [*addrs, EXIT]}
    for a in addrs:
        for s in mem[a].succ():
            preds_rev.setdefault(s, []).append(a)
    # Postorder over the reverse graph from EXIT.
    order: list[int] = []
    seen = {EXIT}
    stack = [(EXIT, iter(preds_rev[EXIT]))]
    while stack:
        node, it = stack[-1]
        for p in it:
            if p not in seen:
                seen.add(p)
                stack.append((p, iter(preds_rev.get(p, []))))
                break
        else:
            stack.pop()
            order.append(node)
    index = {n: i for i, n in enumerate(order)}
    idom: dict[int, int | None] = {EXIT: EXIT}

    def intersect(a: int, b: int) -> int:
        while a != b:
            while index[a] < index[b]:
                a = idom[a]
            while index[b] < index[a]:
                b = idom[b]
        return a

    changed = True
    rpo = [n for n in reversed(order) if n != EXIT]
    while changed:
        changed = False
        for n in rpo:
            cands = [s for s in mem[n].succ() if s in idom and s in index]
            if not cands:
                continue
            new = cands[0]
            for s in cands[1:]:
                new = intersect(s, new)
            if idom.get(n) != new:
                idom[n] = new
                changed = True
    out: dict[int, int | None] = {a: idom.get(a) for a in addrs}
    return out


def region_nodes(mem: list[Instr], start: list[int], stop: int | None) -> set[int]:
    """Addresses reachable from ``start`` without passing ``stop``."""
    seen: set[int] = set()
    stack = [s for s in start if s != stop and s != EXIT]
    while stack:
        a = stack.pop()
        if a in seen:
            continue
        seen.add(a)
        stack.extend(s for s in mem[a].succ() if s not in (stop, EXIT) and s not in seen)
    return seen


def write_table(mem: list[Instr], start: list[int], stop: int | None) -> dict[int, frozenset]:
    """For each address of the region: names writable from it before ``stop``."""
    nodes = region_nodes(mem, start, stop)
    w: dict[int, set] = {a: set(mem[a].writes()) for a in nodes}
    changed = True
    while changed:
        changed = False
        for a in nodes:
            acc = w[a]
            before = len(acc)
            for s in mem[a].succ():
                if s in nodes:
                    acc |= w[s]
            if len(acc) != before:
                changed = True
    return {a: frozenset(v) for a, v in w.items()}


def live_in(mem: list[Instr], start: int, stop: int, at_stop: frozenset) -> frozenset:
    """Names live at ``start`` in the region before ``stop``, where ``stop`` reads ``at_stop``."""
    nodes = region_nodes(mem, [start], stop)
    live: dict[int, frozenset] = {a: frozenset() for a in nodes}
    changed = True
    while changed:
        changed = False
        for a in nodes:
            ins = mem[a]
            out: set = set()
            for s in ins.succ():
                if s == stop:
                    out |= at_stop
                elif s in nodes:
                    out |= live[s]
            new = frozenset(ins.reads()) | (frozenset(out) - frozenset(ins.writes()))
            if new != live[a]:
                live[a] = new
                changed = True
    return live.get(start, frozenset())


class Tables:
    """Reconvergence and may-write tables for one region, built lazily per control instruction."""

    def __init__(self, mem: list[Instr], addrs: list[int], each_body: dict[int, set[int]]):
        self.mem = mem
        self.addrs = addrs
        self.reconv = ipdoms(mem, addrs)
        self.each_body = each_body
        self._writes: dict[int, dict[int, frozenset]] = {}
        self._carried: dict[int, frozenset] = {}
        for a, body in each_body.items():
            ins = mem[a]
            ins.has_exit = any(
                s not in body and s != a
                for b in body
                if mem[b].op != Op.ITER_END
                for s in mem[b].succ()
            )

    def structured(self, a: int) -> bool:
        """A branch / switch / each whose paths rejoin inside its own `each` body (or top level)."""
        r = self.reconv.get(a)
        if r is None or r == EXIT:
            return False
        ins = self.mem[a]
        ctx = ins.each_ctx
        if ctx is None:
            return True
        return r in self.each_body.get(ctx, ())

    def region_writes(self, a: int) -> dict[int, frozenset]:
        """Names writable from each address of the region a control instruction opens."""
        if a not in self._writes:
            ins = self.mem[a]
            r = self.reconv.get(a)
            stop = None if r == EXIT else r
            start = ins.succ()
            table = write_table(self.mem, start, stop)
            if ins.op == Op.EACH:
                extra = frozenset(n for n in (ins.item, ins.index) if n)
                table = {k: v | extra for k, v in table.items()}
                table[a] = frozenset().union(*table.values(), extra) if table else extra
            self._writes[a] = table
        return self._writes[a]

    def may_write(self, a: int) -> frozenset:
        table = self.region_writes(a)
        ins = self.mem[a]
        if ins.op in (Op.BRANCH, Op.SWITCH):
            return frozenset().union(*(table.get(s, frozenset()) for s in ins.succ()))
        return table.get(a, frozenset())

    def body_writes(self, each_addr: int) -> dict[int, frozenset]:
        """For an `each` body: names writable from each body address before ITER_END."""
        key = -each_addr - 2
        if key not in self._writes:
            ins = self.mem[each_addr]
            self._writes[key] = write_table(self.mem, [ins.body], ins.iter_end)
        return self._writes[key]

    def body_may_write(self, each_addr: int) -> frozenset:
        ins = self.mem[each_addr]
        return self.body_writes(each_addr).get(ins.body, frozenset())

    def carried(self, each_addr: int) -> frozenset:
        """Body writes an iteration can see from the one before: live at the body entry.

        The body's end counts as a read of every body write, so a name that
        some path leaves unwritten carries its value to the next iteration and
        out of the loop. With ``ALL`` in the writes, every name is carried.
        """
        if each_addr not in self._carried:
            ins = self.mem[each_addr]
            writes = self.body_may_write(each_addr)
            if ALL in writes:
                self._carried[each_addr] = writes
            else:
                live = live_in(self.mem, ins.body, ins.iter_end, writes)
                self._carried[each_addr] = frozenset(writes & live)
        return self._carried[each_addr]


def has_all(names: frozenset) -> bool:
    return ALL in names
