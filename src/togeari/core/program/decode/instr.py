"""Instructions of code memory: one decoded statement per global address."""

from dataclasses import dataclass, field
from typing import Any

from kohakunode.syntax.ast import Origin

from togeari.core.program.origin import origin_dict

# Operand kinds: ("n", name) reads a name, ("l", value) a literal, ("d", value) a
# signature default, ("missing", port) a required input nobody gave.
Operand = tuple[str, Any]


class Op:
    """Instruction kinds; ``match`` compares against the dotted names (``case Op.CALL``)."""

    CALL = "call"
    ASSIGN = "assign"
    BRANCH = "branch"
    SWITCH = "switch"
    JUMP = "jump"
    EACH = "each"
    ITER_END = "iter_end"
    END = "end"


# Marker in a may-write set: the region holds a `mutates` node, so every name may change
ALL = "*"


@dataclass
class Span:
    """Source location and structured provenance; legacy spans may lack an origin."""

    source: str
    line: int | None
    node: str
    def_of: str | None = None
    origin: Origin | None = None

    def __str__(self) -> str:
        where = f"{self.source}:{self.line}" if self.line else self.source
        via = f" (in @def {self.def_of})" if self.def_of else ""
        calls = ""
        if self.origin is not None and self.origin.calls:
            calls = " via " + " -> ".join(
                f"{call.module}:{call.line}" if call.line else call.module
                for call in self.origin.calls
            )
        return f"{where} {self.node}{via}{calls}"


@dataclass
class Instr:
    addr: int
    op: str
    region: int = 0
    span: Span | None = None
    next: int | None = None
    hint: dict = field(default_factory=dict)
    # call
    node: str = ""
    signature: Any = None
    args: list[tuple[str, Operand]] = field(default_factory=list)
    outs: list[str | None] = field(default_factory=list)
    n_sig_out: int = 0
    ordered: bool = False
    mutates: list[tuple[str, str | None]] = field(default_factory=list)
    binding: Any = None
    # assign
    target: str = ""
    operand: Operand | None = None
    # branch / switch / jump
    targets: list[int] = field(default_factory=list)
    cases: list[tuple[Any, int]] = field(default_factory=list)
    default: int | None = None
    # each
    item: str | None = None
    index: str | None = None
    body: int | None = None
    after: int | None = None
    iter_end: int | None = None
    has_exit: bool = False
    each_addr: int | None = None
    collect_value: Operand | None = None
    collect_target: str | None = None
    # static context
    each_ctx: int | None = None  # innermost `each` whose body holds this instruction
    try_id: tuple | None = None  # innermost `@try` body holding this instruction

    def origin_dict(self) -> dict | None:
        """Detached JSON provenance, also used by the native core's retained instruction."""
        return origin_dict(self.span.origin) if self.span is not None else None

    def record_origin(self, origins: list, index: int) -> None:
        """Append alongside a call record; pad a legacy snapshot's missing prefix."""
        origins.extend([None] * (index - len(origins)))
        origins.append(self.origin_dict())

    def writes(self) -> list[str]:
        match self.op:
            case Op.CALL:
                w = [o for o in self.outs if o is not None]
                return [*w, ALL] if self.mutates else w
            case Op.ASSIGN:
                return [self.target]
            case Op.EACH:
                return [n for n in (self.item, self.index, self.collect_target) if n is not None]
            case _:
                return []

    def reads(self) -> list[str]:
        match self.op, self.operand:
            case Op.CALL, _:
                ops = [o for _, o in self.args]
            case _, None:
                ops = []
            case _, op:
                ops = [op]
        return [o[1] for o in ops if o[0] == "n"]

    def succ(self) -> list[int]:
        """Flow successors; -1 is the exit."""
        match self.op:
            case Op.CALL | Op.ASSIGN:
                return [self.next]
            case Op.BRANCH:
                return list(self.targets)
            case Op.SWITCH:
                s = [a for _, a in self.cases]
                s.append(self.default if self.default is not None else -1)
                return list(dict.fromkeys(s))
            case Op.JUMP:
                return [self.targets[0]]
            case Op.EACH:
                return [self.body, self.after]
            case Op.ITER_END:
                return [self.each_addr]
            case _:
                return [-1]


@dataclass
class TryInfo:
    try_id: tuple
    body: set[int]
    handler: int
    error_name: str | None
    outer: tuple | None
