"""Flatten a lowered job program into instructions of code memory.

Continuations follow KIR's rules: a namespace is skipped in sequence; its body
continues after the namespace definition, except an `each` body (back to its
`each`, through an ITER_END) and `parallel` arms (on to the next arm).
"""

import re
from collections.abc import Callable
from dataclasses import replace

from kohakunode.sig.env import NodeEnv, Resolved
from kohakunode.syntax.ast import (
    Assign,
    Branch,
    Call,
    Dataflow,
    Def,
    Each,
    Jump,
    Literal,
    Name,
    Namespace,
    Parallel,
    Stmt,
    Switch,
    Try,
    TypeHints,
)

from togeari.core.program.decode.instr import Instr, Op, Operand, Span, TryInfo

Target = Callable[[], int]
_DEF_PREFIX = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*?)__\d+__")


def operand(e) -> Operand:
    match e:
        case Name():
            return ("n", e.id)
        case Literal():
            return ("l", e.value)
        case _:
            raise TypeError(f"unknown expression {e!r}")


class Compiler:
    def __init__(self, mem: list[Instr], region: int, env: NodeEnv, source: str):
        self.mem = mem
        self.region = region
        self.env = env
        self.source = source
        self.pending: list[Callable[[], None]] = []
        self.ns_entry: dict[int, Target] = {}
        self.tries: dict[tuple, TryInfo] = {}
        self.n_tries = 0
        self.each_body: dict[int, set[int]] = {}
        self.problems: list[str] = []

    # ------------------------------------------------------------ helpers
    def new(self, op: str, stmt: Stmt | None, each_ctx, try_id, **kw) -> Instr:
        ins = Instr(len(self.mem), op, self.region, **kw)
        ins.each_ctx = each_ctx
        ins.try_id = try_id
        if stmt is not None:
            ins.hint = dict(stmt.hint)
            ins.span = self.span(stmt)
        self.mem.append(ins)
        return ins

    def span(self, s: Stmt) -> Span:
        match s:
            case Call():
                node = s.name
                names = [o for o in s.outs if o]
                names += [a.value.id for a in s.args if isinstance(a.value, Name)]
            case Assign():
                node = f"{s.target} ="
                names = [s.target]
            case _:
                node = type(s).__name__.lower()
                names = []

        # Structured provenance survives linking, inlining and metadata stripping.
        origin = getattr(s, "origin", None)
        if origin is not None:
            return Span(origin.module, origin.line, node, origin.definition, origin)

        # Compatibility for unstamped ASTs: recover the definition from rename prefixes.
        m = next((m for m in (_DEF_PREFIX.match(n) for n in names) if m), None)
        return Span(self.source, s.line, node, m.group(1) if m else None)

    def later(self, fn: Callable[[], None]) -> None:
        self.pending.append(fn)

    # ------------------------------------------------------------ blocks
    def compile(self, body: list[Stmt]) -> int:
        end = self.new(Op.END, None, None, None)
        first = self.block(body, [], lambda: end.addr, None, None)
        for fn in self.pending:
            fn()
        return first()

    def block(self, stmts, scopes, cont: Target, each_ctx, try_id) -> Target:
        local = {s.label: s for s in stmts if isinstance(s, Namespace)}
        chain = [local, *scopes]
        after: list[Target] = [cont] * (len(stmts) + 1)
        made: dict[int, Instr] = {}

        # Statements back to front: each one's entry, given the entry of what follows it
        for i in range(len(stmts) - 1, -1, -1):
            s = stmts[i]
            nxt = after[i + 1]
            match s:
                case Namespace() | Def() | TypeHints():
                    after[i] = nxt
                case Dataflow():
                    after[i] = self.block(s.body, chain, nxt, each_ctx, try_id)
                case Try():
                    after[i] = self.try_block(s, chain, nxt, each_ctx, try_id)
                case _:
                    ins = self.simple(s, each_ctx, try_id)
                    made[i] = ins
                    after[i] = lambda a=ins.addr: a

        # Namespace bodies: their continuation depends on the builtin that owns them
        cont_of: dict[int, Target] = {}
        ctx_of: dict[int, int | None] = {}
        for i, ins in made.items():
            match stmts[i]:
                case Parallel() as s:
                    arms = [local.get(label) for label in s.labels]
                    for k, ns in enumerate(arms):
                        if ns is None:
                            continue
                        nxt_arm = arms[k + 1] if k + 1 < len(arms) else None
                        cont_of[id(ns)] = (
                            self.ns_target(nxt_arm) if nxt_arm is not None else after[i + 1]
                        )
                case Each() as s if (ns := local.get(s.label)) is not None:
                    end = self.new(Op.ITER_END, None, ins.addr, try_id, each_addr=ins.addr)
                    end.span = self.span(s)
                    if end.span.origin is not None:
                        end.span.origin = replace(end.span.origin, generated="iter_end")
                    ins.iter_end = end.addr
                    cont_of[id(ns)] = lambda a=end.addr: a
                    ctx_of[id(ns)] = ins.addr

        # Namespace bodies compiled; an `each` body records its addresses
        for i, s in enumerate(stmts):
            if not isinstance(s, Namespace):
                continue
            ctx = ctx_of.get(id(s), each_ctx)
            start = len(self.mem)
            self.ns_entry[id(s)] = self.block(
                s.body, chain, cont_of.get(id(s), after[i + 1]), ctx, try_id
            )
            if id(s) in ctx_of:
                owner = ctx_of[id(s)]
                body = set(range(start, len(self.mem)))
                body.add(self.mem[owner].iter_end)
                self.each_body[owner] = body

        # Control targets, resolved once every address exists
        for i, ins in made.items():
            self.wire(stmts[i], ins, chain, local, after[i + 1])
        return after[0]

    def try_block(self, s: Try, chain, nxt: Target, each_ctx, try_id) -> Target:
        """Compile a `@try` body and handler; returns the body's entry."""
        self.n_tries += 1
        tid = (self.region, self.n_tries)

        # Body addresses form the try's range; the handler runs under the enclosing try
        start = len(self.mem)
        body_entry = self.block(s.body, chain, nxt, each_ctx, tid)
        end = len(self.mem)
        handler_entry = self.block(s.handler, chain, nxt, each_ctx, try_id)
        info = TryInfo(tid, set(range(start, end)), -1, s.error_name, try_id)
        self.tries[tid] = info

        def fix(info=info, h=handler_entry):
            info.handler = h()

        self.later(fix)
        return body_entry

    def ns_target(self, ns: Namespace) -> Target:
        return lambda: self.ns_entry[id(ns)]()

    def enter(self, label: str, scopes) -> Target:
        for table in scopes:
            if label in table:
                return self.ns_target(table[label])
        self.problems.append(f"namespace `{label}` not found")
        return lambda: -1

    # ------------------------------------------------------------ statements
    def simple(self, s: Stmt, each_ctx, try_id) -> Instr:
        match s:
            case Assign():
                return self.new(
                    Op.ASSIGN, s, each_ctx, try_id, target=s.target, operand=operand(s.value)
                )
            case Call():
                return self.call(s, each_ctx, try_id)

            # Control
            case Branch():
                return self.new(Op.BRANCH, s, each_ctx, try_id, operand=operand(s.cond))
            case Switch():
                return self.new(Op.SWITCH, s, each_ctx, try_id, operand=operand(s.value))
            case Jump() | Parallel():
                return self.new(Op.JUMP, s, each_ctx, try_id)

            # Loops
            case Each():
                return self.new(
                    Op.EACH,
                    s,
                    each_ctx,
                    try_id,
                    operand=operand(s.items),
                    item=s.item,
                    index=s.index,
                )

            case _:
                raise TypeError(f"cannot compile {type(s).__name__}")

    def call(self, s: Call, each_ctx, try_id) -> Instr:
        r = self.env.resolve(s.name)
        if not isinstance(r, Resolved):
            self.problems.append(f"node {s.name!r} is {r}")
            return self.new(Op.CALL, s, each_ctx, try_id, node=s.name, outs=list(s.outs))
        sig = r.signature

        # Positional then keyword arguments, by port name
        args: dict[str, Operand] = {}
        positional = [a for a in s.args if a.key is None]
        for port, a in zip(sig.inputs, positional, strict=False):
            args[port.name] = operand(a.value)
        for a in s.args:
            if a.key is not None:
                args[a.key] = operand(a.value)

        # Ports in signature order; missing ones take their default or a marker
        ordered_args: list[tuple[str, Operand]] = []
        for port in sig.inputs:
            match port.name in args, port.required:
                case True, _:
                    ordered_args.append((port.name, args.pop(port.name)))
                case False, True:
                    ordered_args.append((port.name, ("missing", port.name)))
                case False, False:
                    ordered_args.append((port.name, ("d", port.default)))
        ordered_args += list(args.items())

        # `mutates` ports and the names they change
        by_port = dict(ordered_args)
        mutates = []
        for m in sig.mutates:
            op = by_port.get(m)
            mutates.append((m, op[1] if op and op[0] == "n" else None))

        return self.new(
            Op.CALL,
            s,
            each_ctx,
            try_id,
            node=s.name,
            signature=sig,
            args=ordered_args,
            outs=list(s.outs),
            n_sig_out=len(sig.outputs),
            ordered=sig.ordered,
            mutates=mutates,
        )

    def wire(self, s: Stmt, ins: Instr, chain, local, nxt: Target) -> None:
        match s:
            case Branch():
                t, f = self.enter(s.true_label, [local]), self.enter(s.false_label, [local])

                def fix():
                    ins.targets = [t(), f()]

            case Switch():
                cases = [(v, self.enter(label, [local])) for v, label in s.cases]
                dflt = self.enter(s.default, [local]) if s.default else None

                def fix():
                    ins.cases = [(v, t()) for v, t in cases]
                    ins.default = dflt() if dflt else None

            case Jump():
                t = self.enter(s.label, chain)

                def fix():
                    ins.targets = [t()]

            case Parallel():
                t = self.enter(s.labels[0], [local])

                def fix():
                    ins.targets = [t()]

            case Each():
                body = self.enter(s.label, [local])

                def fix():
                    ins.body, ins.after = body(), nxt()

            case _:

                def fix():
                    ins.next = nxt()

        self.later(fix)
