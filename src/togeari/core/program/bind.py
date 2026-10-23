"""Bindings: node-name patterns -> a lane (with adapter and store mode) or inline (spec/02 §2.4)."""

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from kohakunode.sig.env import Resolved
from kohakunode.sig.std import INTERNAL

from togeari.core.construct.errors import Problems
from togeari.core.contracts.execution import ExecutionPolicy
from togeari.core.program.code import CodeMemory, Region
from togeari.core.program.decode.instr import Instr

STORE_MODES = (None, "content", "positional")


@dataclass
class Use:
    """Bind nodes to a lane (or one engine, as shorthand for a one-unit lane).

    An explicit policy, including an empty one, overrides legacy timeout hints.
    Without one, legacy hints are resolved once at binding time with a warning.
    """

    target: Any
    adapter: Any = None
    store: str | None = None
    idempotent: bool = False
    policy: ExecutionPolicy | None = None


@dataclass
class Inline:
    """Run the node's library function on the dispatcher; warn above a median of ``warn_us``."""

    warn_us: float | None = None


@dataclass
class Bound:
    """What one call instruction is bound to."""

    pattern: str
    inline: bool
    fn: Any = None
    lane: Any = None
    adapter: Any = None
    store: str | None = None
    idempotent: bool = False
    warn_us: float | None = None
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    policy_source: str = "none"


@dataclass
class Defaults:
    """What a binding takes when it names none: the adapter and the units' protocol."""

    adapter: Any
    protocol: str


def unit_protocol(unit: Any, default: str) -> str:
    return getattr(unit, "protocol", None) or default


def _specificity(pattern: str) -> tuple[int, int]:
    wild = any(c in pattern for c in "*?[")
    return (0 if wild else 1, len(pattern.replace("*", "").replace("?", "")))


def names_of(ins: Instr, region: Region) -> list[str]:
    """Match the linked call spelling and its original host-library-qualified name."""
    names = [ins.node]
    r = region.job.env.resolve(ins.node)
    if isinstance(r, Resolved) and r.library is not None:
        names.append(f"{r.library.name}.{r.signature.name}")
    return names


def match(patterns: list[str], names: list[str]) -> tuple[str | None, list[str]]:
    """The most specific matching pattern; ties are returned for the error."""
    hits = [p for p in patterns if any(fnmatch.fnmatchcase(n, p) for n in names)]
    if not hits:
        return None, []
    hits.sort(key=_specificity, reverse=True)
    best = [p for p in hits if _specificity(p) == _specificity(hits[0])]
    return best[0], best


def adapter_for(given: Any, default: Any, fn: Any) -> Any:
    """The given adapter or the default, bound to the node function when it takes one."""
    adapter = given if given is not None else default
    bound = getattr(adapter, "bound", None)
    if bound is not None and (given is None or getattr(adapter, "fn", None) is None):
        return bound(fn)
    return adapter


def check_unit(ins: Instr, adapter: Any, unit: Any, protocol: str) -> list[str]:
    """Problems of running the node ``ins`` through ``adapter`` on ``unit``."""
    out = []
    proto = unit_protocol(unit, protocol)
    if proto not in adapter.protocols:
        out.append(
            f"unit {unit.name!r} speaks {proto!r}; the adapter of {ins.node!r} speaks"
            f" {sorted(adapter.protocols)}"
        )
    if ins.mutates and not getattr(unit, "shares_memory", True):
        outs = {p.name for p in ins.signature.outputs}
        for m, _ in ins.mutates:
            if m not in outs:
                out.append(
                    f"node {ins.node!r} mutates {m!r} but unit {unit.name!r} copies"
                    f" (shares_memory=False) and {m!r} is not an output"
                )
    return out


def _execution_policy(
    explicit: ExecutionPolicy | None, hint: dict, problems: Problems, where: str
) -> tuple[ExecutionPolicy, str]:
    """Resolve lane policy once; malformed legacy values become construction diagnostics."""
    if explicit is not None:
        if not isinstance(explicit, ExecutionPolicy):
            problems.add(where, "Use.policy must be an ExecutionPolicy or None")
            return ExecutionPolicy(), "none"
        if "timeout_s" in hint:
            problems.warn(where, "binding ExecutionPolicy overrides legacy @hint timeout_s")
        return explicit, "binding"
    if "timeout_s" not in hint:
        return ExecutionPolicy(), "none"
    problems.warn(
        where, "legacy @hint timeout_s is deprecated; use Use(..., policy=ExecutionPolicy(...))"
    )
    try:
        value = hint["timeout_s"]
        # Preserve historical conversion (including numeric strings and bools),
        # then enforce the new finite/nonnegative construction-time contract.
        policy = ExecutionPolicy(None if value is None else float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        problems.add(where, f"invalid legacy @hint timeout_s: {exc}")
        return ExecutionPolicy(), "none"
    return policy, "legacy_hint"


def _warn_inline_timeout(hint: dict, problems: Problems, where: str) -> None:
    if "timeout_s" in hint:
        problems.warn(where, "legacy @hint timeout_s is ignored for Inline execution")


def bind_region(
    code: CodeMemory,
    region: Region,
    bind: dict[str, Any],
    lane_of: Any,
    problems: Problems,
    used: set[str],
    defaults: Defaults,
) -> None:
    """Resolve every call of a region; collect every problem."""
    patterns = list(bind)
    src = region.job.source
    for ins in code.calls(region):
        if ins.signature is None:
            problems.add(src, f"node {ins.node!r} is unknown")
            continue
        resolved = region.job.env.resolve(ins.node)
        where = str(ins.span) if ins.span else src
        if (
            ins.node in ("kir.collect_items", "kir.collect_append")
            and isinstance(resolved, Resolved)
            and resolved.library is INTERNAL
        ):
            _warn_inline_timeout(ins.hint, problems, where)
            ins.binding = Bound(ins.node, True, fn=region.job.env.impl(ins.node))
            continue
        names = names_of(ins, region)
        pat, ties = match(patterns, names)
        if pat is None:
            problems.add(where, f"node {ins.node!r} is not bound")
            continue
        if len(ties) > 1:
            problems.add(
                "bind", f"node {ins.node!r} matches {' and '.join(map(repr, ties))} equally"
            )
            continue
        used.add(pat)
        b = bind[pat]
        fn = region.job.env.impl(ins.node)

        # Inline: the library function on the dispatcher
        if isinstance(b, Inline):
            _warn_inline_timeout(ins.hint, problems, where)
            if fn is None:
                problems.add(where, f"node {ins.node!r} is inline but has no Python implementation")
                continue
            ins.binding = Bound(pat, True, fn=fn, warn_us=b.warn_us)
            continue

        # A lane: adapter and units must agree
        if not isinstance(b, Use):
            b = Use(b)
        policy, policy_source = _execution_policy(b.policy, ins.hint, problems, where)
        lane = lane_of(b.target)
        adapter = adapter_for(b.adapter, defaults.adapter, fn)
        for msg in adapter.check(ins.signature):
            problems.add(where, msg)
        for unit in lane.units:
            for msg in check_unit(ins, adapter, unit, defaults.protocol):
                problems.add(f"lane {lane.name!r}", msg)
        store = b.store
        if store not in STORE_MODES:
            problems.add(where, f"store={store!r} is not one of {STORE_MODES}")
            store = None
        if ins.ordered and store is not None:
            problems.warn("bind", f"node {ins.node!r} is ordered; its store={store!r} is ignored")
            store = None
        ins.binding = Bound(
            pat,
            False,
            fn=fn,
            lane=lane,
            adapter=adapter,
            store=store,
            idempotent=b.idempotent,
            policy=policy,
            policy_source=policy_source,
        )
