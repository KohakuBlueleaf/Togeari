"""Bindings: node-name patterns -> a lane (with adapter and store mode) or inline (spec/02 §2.4)."""

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from kohakunode.sig.env import Resolved
from kohakunode.sig.std import INTERNAL

from togeari.core.construct.errors import Problems
from togeari.core.contracts.execution import ExecutionPolicy
from togeari.core.contracts.reuse import builtin_revision
from togeari.core.program.code import CodeMemory, Region
from togeari.core.program.decode.instr import Instr

STORE_MODES = (None, "content", "positional")


@dataclass
class Use:
    """Bind nodes to a lane (or one engine, as shorthand for a one-unit lane).

    An explicit policy, including an empty one, overrides legacy timeout hints.
    Without one, legacy hints are resolved once at binding time with a warning.
    ``implementation_revision`` identifies the host implementation only;
    ``binding_revision`` attests the whole host/adapter/engine/semantic-config bundle.
    Neither revision declares determinism. ``deterministic`` separately authorizes
    content/squash reuse, subject to the ordered and mutation safeguards.
    """

    target: Any
    adapter: Any = None
    store: str | None = None
    idempotent: bool = False
    policy: ExecutionPolicy | None = None
    implementation_revision: str | None = None
    binding_revision: str | None = None
    deterministic: bool | None = None


@dataclass
class Inline:
    """Run the library function on the dispatcher; warn above a median of ``warn_us``.

    Optional host and whole-bundle revision attestations have the same scope as ``Use``.
    """

    warn_us: float | None = None
    implementation_revision: str | None = None
    binding_revision: str | None = None
    deterministic: bool | None = None


@dataclass(frozen=True)
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
    implementation_revision: str | None = None
    binding_revision: str | None = None
    deterministic: bool = False
    reuse_source: str = "none"


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


def _revision(value: Any, field: str, problems: Problems, where: str) -> str | None:
    """Opaque explicit attestations; do not trim or infer revisions from objects."""
    if value is None or (type(value) is str and value):
        return value
    problems.add(where, f"{field} must be a nonempty string or None")
    return None


def _reuse(
    explicit: Any, store: str | None, resolved: Any, fn: Any, problems: Problems, where: str
) -> tuple[bool, str]:
    """Normalize eligibility once; legacy explicit content storage is a warned assertion."""
    if explicit is not None:
        if type(explicit) is not bool:
            problems.add(where, "deterministic must be a bool or None")
            return False, "none"
        if explicit is False and store == "content":
            problems.add(where, "store='content' conflicts with deterministic=False")
        return explicit, "binding"
    if store == "content":
        problems.warn(
            where,
            "store='content' without deterministic is a legacy determinism assertion; "
            "set deterministic=True explicitly, or disable content reuse for sampling nodes",
        )
        return True, "legacy_content"
    if builtin_revision(resolved, fn) is not None:
        return True, "builtin"
    return False, "none"


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
            fn = region.job.env.impl(ins.node)
            # Generated helpers stay inline, but a matching declaration may disable reuse.
            pat, ties = match(patterns, names_of(ins, region))
            if len(ties) > 1:
                problems.add("bind", f"node {ins.node!r} matches multiple bindings equally")
                continue
            declaration = bind[pat] if pat is not None else None
            if pat is not None:
                used.add(pat)
            deterministic, reuse_source = _reuse(
                getattr(declaration, "deterministic", None), None, resolved, fn, problems, where
            )
            ins.binding = Bound(
                pat or ins.node,
                True,
                fn=fn,
                implementation_revision=_revision(
                    getattr(declaration, "implementation_revision", None),
                    "implementation_revision",
                    problems,
                    where,
                ),
                binding_revision=_revision(
                    getattr(declaration, "binding_revision", None),
                    "binding_revision",
                    problems,
                    where,
                ),
                deterministic=deterministic,
                reuse_source=reuse_source,
            )
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
        implementation_revision = _revision(
            getattr(b, "implementation_revision", None), "implementation_revision", problems, where
        )
        binding_revision = _revision(
            getattr(b, "binding_revision", None), "binding_revision", problems, where
        )

        # Inline: the library function on the dispatcher
        if isinstance(b, Inline):
            _warn_inline_timeout(ins.hint, problems, where)
            if fn is None:
                problems.add(where, f"node {ins.node!r} is inline but has no Python implementation")
                continue
            deterministic, reuse_source = _reuse(
                b.deterministic, None, resolved, fn, problems, where
            )
            ins.binding = Bound(
                pat,
                True,
                fn=fn,
                warn_us=b.warn_us,
                implementation_revision=implementation_revision,
                binding_revision=binding_revision,
                deterministic=deterministic,
                reuse_source=reuse_source,
            )
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
        deterministic, reuse_source = _reuse(b.deterministic, store, resolved, fn, problems, where)
        if store is not None and (ins.ordered or ins.mutates):
            effect = "ordered" if ins.ordered else "mutating"
            problems.warn("bind", f"node {ins.node!r} is {effect}; its store={store!r} is ignored")
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
            implementation_revision=implementation_revision,
            binding_revision=binding_revision,
            deterministic=deterministic,
            reuse_source=reuse_source,
        )
