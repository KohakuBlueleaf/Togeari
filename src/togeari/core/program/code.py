"""Code memory: every loaded job program, decoded into regions of one instruction table.

A job carries only its instruction addresses; the dispatcher never asks which
program a job belongs to. Regions are cached by semantic hash, never moved and
never rewritten, so loading a program while jobs run affects none of them.

Loading is staged: ``stage`` decodes a job program into new addresses that no
entry reaches yet, and ``commit`` makes the region known (lookup, cache,
`@try` table). A region that never commits is never cached.
"""

import itertools
from bisect import bisect_right, insort
from dataclasses import dataclass, field

from togeari.core.program.decode.analysis import Tables
from togeari.core.program.decode.compiler import Compiler
from togeari.core.program.decode.instr import Instr, Op, TryInfo
from togeari.core.program.job import JobProgram


@dataclass
class Region:
    index: int
    job: JobProgram
    start: int
    end: int
    entry_addr: int
    tables: Tables
    tries: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def semantic_hash(self) -> str:
        return self.job.semantic_hash


@dataclass
class Entry:
    """A named address where jobs start, with its input and output fields."""

    name: str
    addr: int
    region: Region

    @property
    def params(self):
        return self.region.job.params

    @property
    def outputs(self):
        return self.region.job.outputs


class CodeMemory:
    def __init__(self) -> None:
        self.mem: list[Instr] = []
        self.regions: list[Region] = []
        self.by_hash: dict[str, Region] = {}
        self.entries: dict[str, Entry] = {}
        self.tries: dict[tuple, TryInfo] = {}
        self._starts: list[int] = []
        self._by_start: dict[int, Region] = {}
        self._index = itertools.count()

    # ------------------------------------------------------------ loading
    def cached(self, job: JobProgram) -> Region | None:
        return self.by_hash.get(job.semantic_hash)

    def stage(self, job: JobProgram) -> Region:
        """Decode a job program into new addresses; its problems are in ``region.problems``."""
        start = len(self.mem)
        comp = Compiler(self.mem, next(self._index), job.env, job.source)
        entry_addr = comp.compile(job.program.body)
        tables = Tables(self.mem, list(range(start, len(self.mem))), comp.each_body)
        return Region(
            comp.region, job, start, len(self.mem), entry_addr, tables, comp.tries, comp.problems
        )

    def commit(self, region: Region) -> None:
        """Make a staged region known: address lookup, `@try` table, cache by hash."""
        if region.start in self._by_start:
            return
        self.tries.update(region.tries)
        self.regions.append(region)
        insort(self._starts, region.start)
        self._by_start[region.start] = region
        self.by_hash[region.semantic_hash] = region

    def discard(self, region: Region) -> bool:
        """Drop a staged region that was never committed: its instructions leave memory when
        they are its tail (a later staged region keeps them, unreachable). True if dropped."""
        if region.start in self._by_start or region.end != len(self.mem):
            return False
        del self.mem[region.start :]
        return True

    def load(self, job: JobProgram, problems: list[str] | None = None) -> Region:
        """The cached region of a job program, or a new one, committed when it decodes cleanly."""
        cached = self.cached(job)
        if cached is not None:
            return cached
        region = self.stage(job)
        if problems is not None:
            problems.extend(region.problems)
        if not region.problems:
            self.commit(region)
        return region

    def add_entry(self, name: str, region: Region) -> Entry:
        e = Entry(name, region.entry_addr, region)
        self.entries[name] = e
        return e

    # ------------------------------------------------------------ lookup
    def region_of(self, addr: int) -> Region:
        i = bisect_right(self._starts, addr) - 1
        if i >= 0:
            r = self._by_start[self._starts[i]]
            if addr < r.end:
                return r
        raise KeyError(addr)

    def tables(self, addr: int) -> Tables:
        return self.region_of(addr).tables

    def calls(self, region: Region):
        for a in range(region.start, region.end):
            ins = self.mem[a]
            if ins.op == Op.CALL:
                yield ins

    def dump(self, region: Region) -> str:
        """The decode tables of one region, as text (``togeari decode``)."""
        t = region.tables
        lines = [
            f"region {region.index}  {region.job.name}  hash {region.semantic_hash[:16]}",
            f"entry @{region.entry_addr}  inputs {[p.name for p in region.job.params]}"
            f"  outputs {region.job.outputs}",
        ]
        for a in range(region.start, region.end):
            ins = self.mem[a]
            extra = []

            # Control instructions: reconvergence and what their region may write
            if ins.op in (Op.BRANCH, Op.SWITCH, Op.EACH):
                extra.append(f"reconv={t.reconv.get(a)}")
                extra.append("structured" if t.structured(a) else "unstructured")
                extra.append(f"may_write={sorted(t.may_write(a))}")
            if ins.op == Op.EACH:
                extra.append(f"body_writes={sorted(t.body_may_write(a))}")
                extra.append(f"carried={sorted(t.carried(a))}")
                extra.append(f"has_exit={ins.has_exit}")

            # Static context
            if ins.try_id is not None:
                extra.append(f"try={ins.try_id}")
            if ins.each_ctx is not None:
                extra.append(f"each_ctx={ins.each_ctx}")
            where = f"  ; {ins.span}" if ins.span else ""
            lines.append(f"{a:5d}  {_describe(ins):<48} {' '.join(extra)}{where}")

        # `@try` bodies and handlers
        for tid, info in sorted(region.tries.items()):
            first, last = (min(info.body), max(info.body)) if info.body else ("-", "-")
            lines.append(
                f"try {tid}: body {first}..{last} handler @{info.handler} error {info.error_name}"
            )
        return "\n".join(lines)


def _fmt(op) -> str:
    match op:
        case ("n", name):
            return name
        case ("missing", _):
            return "<missing>"
        case (_, value):
            return repr(value)


def _describe(ins: Instr) -> str:
    match ins.op:
        case Op.CALL:
            args = ", ".join(_fmt(o) for _, o in ins.args)
            outs = ", ".join(o or "_" for o in ins.outs)
            flags = " ordered" if ins.ordered else ""
            flags += f" mutates{[m for m, _ in ins.mutates]}" if ins.mutates else ""
            return f"({args}){ins.node}({outs}) -> {ins.next}{flags}"
        case Op.ASSIGN:
            return f"{ins.target} = {_fmt(ins.operand)} -> {ins.next}"
        case Op.BRANCH:
            return f"branch {_fmt(ins.operand)} ? {ins.targets[0]} : {ins.targets[1]}"
        case Op.SWITCH:
            cases = ", ".join(f"{v!r}=>{a}" for v, a in ins.cases)
            return f"switch {_fmt(ins.operand)} [{cases}] default {ins.default}"
        case Op.JUMP:
            return f"jump {ins.targets[0]}"
        case Op.EACH:
            return (
                f"each {_fmt(ins.operand)} as ({ins.item}, {ins.index}) body {ins.body}"
                f" end {ins.iter_end} after {ins.after}"
            )
        case Op.ITER_END:
            return f"iter_end of {ins.each_addr}"
        case _:
            return "end"
