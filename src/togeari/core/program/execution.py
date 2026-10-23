"""Versioned declared execution identity, independent of placement and source provenance.

A revision is an attestation, not a code fingerprint or a determinism promise. Unknown
portable values remain unknown; neither pickle nor repr is an identity fallback. Runtime
persistence consumers own warnings, job failures and state installation, not this module.
"""

import copy
import hashlib
import json
import logging
import math
import struct
from typing import Any

from kohakunode.sig.env import Resolved
from kohakunode.sig.model import REQUIRED
from kohakunode.sig.records import thaw_default

from togeari.core.construct.errors import ConstructionError, Problem
from togeari.core.contracts.adapter import PythonAdapter
from togeari.core.contracts.events import EventOutput
from togeari.core.contracts.reuse import builtin_revision
from togeari.core.lanes.lane import Lane
from togeari.core.policies.resources import resource_former, resource_policy
from togeari.core.program.bind import Inline, Use

SCHEMA = "togeari.execution"
VERSION = 1
LAYOUT_VERSION = 1
MODES = ("warn", "strict")
log = logging.getLogger("togeari.persistence")

# Bump this maintained adapter revision when its implementation semantics change.
_ADAPTER_REVISION = "togeari.PythonAdapter/1"


def canonical_bytes(value: Any) -> bytes:
    """Canonical JSON for an already normalized manifest, never arbitrary host objects."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _label(value: Any) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _unknown(unknown: list, path: str, reason: str) -> dict:
    unknown.append({"path": path, "reason": reason})
    return {"unknown": reason}


def _literal(
    value: Any, unknown: list, path: str, active: set | None = None, depth: int = 0
) -> Any:
    """Typed portable constants; preserve huge ints, float bits and container ordering."""
    kind = type(value)
    if value is None or kind in (bool, str):
        return [kind.__name__, value]
    if kind is int:
        return ["int", hex(value)]
    if kind is float:
        return ["float64", struct.pack("!d", value).hex()]
    if kind is bytes:
        return ["bytes", value.hex()]
    if kind not in (list, tuple, dict):
        return _unknown(unknown, path, f"nonportable value of type {_label(value)}")
    active = set() if active is None else active
    if id(value) in active:
        return _unknown(unknown, path, "shared or cyclic container in default or literal")
    if depth >= 128:
        return _unknown(unknown, path, "default or literal nesting exceeds portable identity limit")
    active.add(id(value))
    if kind is dict:
        # Dict insertion order and shared container identity can be observed by a node.
        values = [
            [
                _literal(k, unknown, f"{path}.keys[{i}]", active, depth + 1),
                _literal(v, unknown, f"{path}.values[{i}]", active, depth + 1),
            ]
            for i, (k, v) in enumerate(value.items())
        ]
    else:
        values = [
            _literal(v, unknown, f"{path}[{i}]", active, depth + 1) for i, v in enumerate(value)
        ]
    return [kind.__name__, values]


def _type(value: Any) -> dict:
    return {"kind": value.kind, "name": value.name, "args": [_type(x) for x in value.args]}


def _signature(sig: Any, unknown: list, path: str) -> Any:
    if sig is None:
        return _unknown(unknown, path, "unresolved signature")
    result = {"name": sig.name, "ordered": sig.ordered, "mutates": list(sig.mutates)}
    for side in ("inputs", "outputs"):
        result[side] = [
            {
                "name": p.name,
                "type": _type(p.type),
                "required": p.default is REQUIRED,
                "default": (
                    None
                    if p.default is REQUIRED
                    else _literal(p.default, unknown, f"{path}.{side}[{i}].default")
                ),
            }
            for i, p in enumerate(getattr(sig, side))
        ]
    return result


def _contracts(job: Any, nodes: list[str], unknown: list) -> dict:
    calls = []
    for i, node in enumerate(nodes):
        resolved = job.env.resolve(node)
        sig = resolved.signature if isinstance(resolved, Resolved) else None
        calls.append(_signature(sig, unknown, f"signatures.calls[{i}]"))
    records = []
    for name, schema in sorted(job.env.record_schemas().items()):
        records.append(
            {
                "id": name,
                "fields": [
                    {
                        "name": f.name,
                        "type": _type(f.type),
                        "has_default": f.has_default,
                        "default": (
                            _literal(
                                thaw_default(f.default),
                                unknown,
                                f"signatures.records.{name}.{f.name}",
                            )
                            if f.has_default
                            else None
                        ),
                    }
                    for f in schema.fields
                ],
            }
        )
    params = [
        {
            "name": p.name,
            "has_default": p.has_default,
            "default": (
                _literal(p.default, unknown, f"signatures.entry.{p.name}")
                if p.has_default
                else None
            ),
        }
        for p in job.params
    ]
    entry = job.env.resolve(job.entry) if job.entry is not None else None
    return {
        "calls": calls,
        "records": records,
        "params": params,
        "outputs": job.outputs,
        "entry": (
            _signature(entry.signature, unknown, "signatures.entry")
            if isinstance(entry, Resolved)
            else None
        ),
    }


def signature_hash(job: Any, nodes: list[str]) -> str:
    """Portable contract description fingerprint (unknown markers are not certification)."""
    return _hash(_contracts(job, nodes, []))


def _offset(region: Any, addr: int | None) -> int | None:
    if addr is None or addr == -1:
        return addr
    if not region.start <= addr < region.end:
        raise ValueError("instruction address crosses its region")
    return addr - region.start


def _try(region: Any, tid: tuple | None) -> int | None:
    if tid is None:
        return None
    if tid[0] != region.index:
        raise ValueError("try context crosses its region")
    return tid[1]


def _layout(region: Any, code: Any, unknown: list) -> dict:
    rows = []
    for ins in code.mem[region.start : region.end]:
        path = f"layout.instructions[{ins.addr - region.start}]"
        row = {
            "position": ins.addr - region.start,
            "op": ins.op,
            "node": ins.node,
            "args": _literal(ins.args, unknown, f"{path}.args"),
            "outs": ins.outs,
            "n_sig_out": ins.n_sig_out,
            "ordered": ins.ordered,
            "mutates": _literal(ins.mutates, unknown, f"{path}.mutates"),
            "target": ins.target,
            "operand": _literal(ins.operand, unknown, f"{path}.operand"),
            "targets": [_offset(region, a) for a in ins.targets],
            "cases": [
                [_literal(v, unknown, f"{path}.cases[{i}]"), _offset(region, a)]
                for i, (v, a) in enumerate(ins.cases)
            ],
            "item": ins.item,
            "index": ins.index,
            "has_exit": ins.has_exit,
            "collect_value": _literal(ins.collect_value, unknown, f"{path}.collect_value"),
            "collect_target": ins.collect_target,
            "try_id": _try(region, ins.try_id),
            "signature": (
                _signature(ins.signature, unknown, f"{path}.signature")
                if ins.signature is not None
                else None
            ),
        }
        for field in ("next", "default", "body", "after", "iter_end", "each_addr", "each_ctx"):
            row[field] = _offset(region, getattr(ins, field))
        rows.append(row)
    return {
        "version": LAYOUT_VERSION,
        "entry_offset": _offset(region, region.entry_addr),
        "instructions": rows,
        "reconv": [
            [_offset(region, a), _offset(region, b)]
            for a, b in sorted(region.tables.reconv.items())
        ],
        "each_body": [
            [_offset(region, a), [_offset(region, b) for b in sorted(body)]]
            for a, body in sorted(region.tables.each_body.items())
        ],
        "tries": [
            {
                "id": _try(region, tid),
                "body": [_offset(region, a) for a in sorted(info.body)],
                "handler": _offset(region, info.handler),
                "error_name": info.error_name,
                "outer": _try(region, info.outer),
            }
            for tid, info in sorted(region.tries.items())
        ],
    }


def layout_hash(region: Any, code: Any) -> str:
    """Normalized decoder layout; absolute PCs, source metadata and hints are excluded."""
    return _hash(_layout(region, code, []))


def _builtin(region: Any, ins: Any) -> str | None:
    return builtin_revision(region.job.env.resolve(ins.node), ins.binding.fn)


def _eligible_units(lane: Any) -> list:
    """Draining/detached units cannot take new calls; down units may recover unchanged."""
    states = lane.state
    return [
        unit
        for unit in lane.units
        if getattr(states.get(id(unit)), "state", None) not in ("draining", "detached")
    ]


def initial_binding_units(bind: dict) -> dict[str, tuple[int, ...]]:
    """Private machine-lifetime attestation baseline, including not-yet-used bindings."""
    result = {}
    for pattern, binding in bind.items():
        if isinstance(binding, Inline):
            continue
        target = binding.target if isinstance(binding, Use) else binding
        units = _eligible_units(target) if isinstance(target, Lane) else [target]
        result[pattern] = tuple(sorted({id(unit) for unit in units}))
    return result


def _number(value: Any, unknown: list, path: str) -> Any:
    if value is None or type(value) in (bool, int):
        return value if type(value) is not int or value.bit_length() < 64 else {"int": hex(value)}
    if type(value) is float:
        if math.isfinite(value):
            return int(value) if value.is_integer() and abs(value) < 2**53 else value
        return {"float64": struct.pack("!d", value).hex()}
    return _unknown(unknown, path, "nonportable policy value")


def _static_execution(region: Any, code: Any) -> dict:
    """Normalize immutable compiled code once, not once per persisted call."""
    if region.execution_static is None:
        unknown: list[dict] = []
        instructions = tuple(code.calls(region))
        contracts = _contracts(region.job, [ins.node for ins in instructions], unknown)
        layout = _layout(region, code, unknown)
        lanes = {}
        for ins in instructions:
            b = ins.binding
            if b is not None and not b.inline:
                group = lanes.setdefault(id(b.lane), {"lane": b.lane, "positions": []})
                group["positions"].append(ins.addr - region.start)
        region.execution_static = {
            "instructions": instructions,
            "emits": tuple(ins for ins in instructions if ins.semantic_emit),
            "lanes": lanes,
            "unknown": unknown,
            "program": {
                "semantic_hash": region.semantic_hash,
                "dependency_hash": region.job.dependencies.get("hash"),
                "entry": region.job.entry,
                "passes": list(region.job.passes),
                "signature_hash": _hash(contracts),
            },
            "layout": {
                "version": LAYOUT_VERSION,
                "instruction_count": region.end - region.start,
                "entry_offset": region.entry_addr - region.start,
                "hash": _hash(layout),
            },
            "signatures": {
                ins.addr: _hash(_signature(ins.signature, [], "signature")) for ins in instructions
            },
            "builtins": {
                ins.addr: _builtin(region, ins) if ins.binding is not None else None
                for ins in instructions
            },
        }
    return region.execution_static


def build_execution(region: Any, code: Any, machine: Any) -> dict:
    """Detached JSON manifest for external reports; hot runtime paths use current_execution."""
    return copy.deepcopy(current_execution(region, code, machine))


def _render_execution(region: Any, code: Any, machine: Any) -> dict:
    """Rebuild dynamic call configuration only when its small observation changes."""
    static = _static_execution(region, code)
    unknown = copy.deepcopy(static["unknown"])
    calls = []
    for ins in static["instructions"]:
        b = ins.binding
        position = ins.addr - region.start
        path = f"calls[{position}]"
        if b is None:
            _unknown(unknown, path, "unbound call")
            continue
        builtin = static["builtins"][ins.addr]
        bundle = b.binding_revision
        invalidated = position in getattr(machine, "_execution_invalidated", {}).get(
            region.index, set()
        )
        if not b.inline:
            baseline = machine._binding_unit_baselines.get(b.pattern)
            membership = tuple(sorted({id(unit) for unit in _eligible_units(b.lane)}))
            invalidated = invalidated or b.pattern in machine._execution_invalidated_bindings
            invalidated = invalidated or id(b.lane) in machine._execution_invalidated_lanes
            invalidated = invalidated or (baseline is not None and baseline != membership)
        covered = bool(bundle) and not invalidated
        if invalidated:
            _unknown(
                unknown,
                f"{path}.binding_revision",
                "eligible units changed since bundle attestation",
            )
        adapter = None
        engines = []
        if not (covered or b.implementation_revision or builtin):
            _unknown(
                unknown, f"{path}.implementation_revision", "host implementation revision unknown"
            )
        if not b.inline:
            revision = _ADAPTER_REVISION if type(b.adapter) is PythonAdapter else None
            adapter = {"label": _label(b.adapter), "revision": revision}
            engines = [
                {"label": name, "revision": None}
                for name in sorted({_label(u) for u in _eligible_units(b.lane)})
            ]
            if not covered:
                if revision is None:
                    _unknown(
                        unknown, f"{path}.adapter", "adapter implementation/configuration unknown"
                    )
                _unknown(unknown, f"{path}.engines", "engine implementation/configuration unknown")
        lane = None if b.inline else b.lane
        retry = None if lane is None else lane.retry
        resource = None if lane is None else resource_policy(lane.former)
        estimator_changed = id(lane) in getattr(machine, "_execution_estimator_invalidated", set())
        if (
            resource is not None
            and resource["estimator_revision"] is None
            and (not covered or estimator_changed)
        ):
            _unknown(
                unknown,
                f"{path}.policy.resource_budget.estimator_revision",
                (
                    "resource estimator changed since bundle attestation"
                    if estimator_changed
                    else "resource estimator revision unknown; a binding revision may attest it"
                ),
            )
        calls.append(
            {
                "position": position,
                "node": ins.node,
                "signature_hash": static["signatures"][ins.addr],
                "implementation_revision": b.implementation_revision,
                "binding_revision": bundle,
                "builtin_revision": builtin,
                "mode": "inline" if b.inline else "lane",
                "adapter": adapter,
                "engines": engines,
                "policy": {
                    **({"resource_budget": resource} if resource is not None else {}),
                    "timeout_s": _number(b.policy.timeout_s, unknown, f"{path}.policy.timeout_s"),
                    "attempt_timeout_s": _number(
                        None if lane is None else lane.timeout_s,
                        unknown,
                        f"{path}.policy.attempt_timeout_s",
                    ),
                    "retry": (
                        None
                        if retry is None
                        else {
                            "max": _number(retry.max, unknown, f"{path}.policy.retry.max"),
                            "backoff_ms": _number(
                                retry.backoff_ms, unknown, f"{path}.policy.retry.backoff_ms"
                            ),
                        }
                    ),
                    "lane_dead_s": _number(
                        None if lane is None else lane.lane_dead_s,
                        unknown,
                        f"{path}.policy.lane_dead_s",
                    ),
                },
                "deterministic": b.deterministic,
                "reuse": {
                    "content": b.deterministic and not ins.ordered and not ins.mutates,
                    "squash": b.deterministic and not ins.ordered and not ins.mutates,
                    "positional": not ins.ordered and not ins.mutates,
                },
                "effects": {
                    "ordered": ins.ordered,
                    "mutates": [list(m) for m in ins.mutates],
                    "idempotent": b.idempotent,
                },
            }
        )
    identity = {
        "program": static["program"],
        "layout": static["layout"],
        "calls": calls,
        "runtime_policy": {
            "step_budget": _number(
                machine.knobs.get("step_budget"), unknown, "runtime_policy.step_budget"
            )
        },
    }
    if static["emits"]:
        output = getattr(machine, "event_output", None)
        if type(output) is EventOutput:
            identity["runtime_policy"]["event_output"] = output.descriptor()
            if output.sink_revision is None and not all(
                ins.binding.binding_revision for ins in static["emits"]
            ):
                _unknown(
                    unknown,
                    "runtime_policy.event_output.sink_revision",
                    "event sink implementation revision unknown",
                )
        else:
            identity["runtime_policy"]["event_output"] = _unknown(
                unknown, "runtime_policy.event_output", "semantic emission has no EventOutput"
            )
    # A fresh job is not a mixed-history job, even after a known policy change.
    transitions = []
    manifest = {"schema": SCHEMA, "version": VERSION, "identity": identity}
    manifest["hash"] = _hash(manifest)
    manifest["completeness"] = {
        "complete": not unknown and not transitions,
        "unknown": unknown,
        "transitions": transitions,
    }
    return manifest


def observe_execution(region: Any, code: Any, machine: Any) -> dict:
    """Borrow a read-only live manifest; unchanged cost is proportional to distinct lanes.

    Callers exposing a manifest or retaining it in mutable job stats must copy it once.
    Object IDs and observations are private process-local guards, never portable identity.
    """
    static = _static_execution(region, code)
    lanes = static["lanes"]
    samples = {}
    if not hasattr(machine, "_execution_estimators"):
        machine._execution_estimators = {}
        machine._execution_estimator_invalidated = set()
    for key, group in lanes.items():
        lane = group["lane"]
        budget = resource_former(lane.former)
        estimate = None if budget is None else budget.estimate
        if (
            key in machine._execution_estimators
            and machine._execution_estimators[key] is not estimate
        ):
            machine._execution_estimator_invalidated.add(key)
        machine._execution_estimators[key] = estimate
        samples[key] = {
            "policy": canonical_bytes(
                [
                    _number(value, [], field)
                    for field, value in (
                        ("timeout_s", lane.timeout_s),
                        ("lane_dead_s", lane.lane_dead_s),
                        ("retry.max", lane.retry.max),
                        ("retry.backoff_ms", lane.retry.backoff_ms),
                    )
                ]
            ),
            "units": tuple(sorted({id(unit) for unit in _eligible_units(lane)})),
            "resource_policy": canonical_bytes(resource_policy(lane.former)),
            "estimator": id(estimate),
        }
    samples["runtime_policy"] = [
        _number(machine.knobs.get("step_budget"), [], "step_budget"),
        (
            getattr(machine, "event_output", None)._descriptor
            if static["emits"] and type(getattr(machine, "event_output", None)) is EventOutput
            else None
        ),
    ]
    previous = machine._execution_observed.get(region.index)
    epoch = machine._execution_epochs.setdefault(region.index, 0)
    coverage = tuple(
        (
            key in machine._execution_invalidated_lanes,
            key in machine._execution_estimator_invalidated,
        )
        for key in lanes
    )
    if (
        previous == samples
        and region.execution_current is not None
        and region.execution_coverage == coverage
    ):
        return {"manifest": region.execution_current, "epoch": epoch}
    changes = {"policy_changed": [], "unit_membership": []}
    if previous is not None:
        for key, group in lanes.items():
            for field, kind in (
                ("policy", "policy_changed"),
                ("units", "unit_membership"),
                ("resource_policy", "policy_changed"),
                ("estimator", "policy_changed"),
            ):
                if previous[key][field] != samples[key][field]:
                    changes[kind].extend(group["positions"])
        if previous["runtime_policy"] != samples["runtime_policy"]:
            changes["policy_changed"] = [
                ins.addr - region.start for ins in static["instructions"]
            ] or [-1]
    machine._execution_observed[region.index] = samples
    if any(changes.values()):
        epoch += 1
        machine._execution_epochs[region.index] = epoch
        history = machine._execution_transitions.setdefault(region.index, [])
        for kind, positions in changes.items():
            if positions:
                history.append({"epoch": epoch, "kind": kind, "positions": sorted(set(positions))})
        if changes["unit_membership"]:
            affected = set(changes["unit_membership"])
            machine._execution_invalidated.setdefault(region.index, set()).update(affected)
            for ins in static["instructions"]:
                if ins.addr - region.start in affected:
                    machine._execution_invalidated_bindings.add(ins.binding.pattern)
                    machine._execution_invalidated_lanes.add(id(ins.binding.lane))
    manifest = _render_execution(region, code, machine)
    manifest["epoch"] = epoch
    region.execution_current = manifest
    region.execution_coverage = tuple(
        (
            key in machine._execution_invalidated_lanes,
            key in machine._execution_estimator_invalidated,
        )
        for key in lanes
    )
    # Only internally built self-validation is cached; external candidates always validate.
    region.execution_decisions = {mode: compatibility(manifest, manifest, mode) for mode in MODES}
    return {"manifest": manifest, "epoch": epoch}


def current_execution(region: Any, code: Any, machine: Any) -> dict:
    """Borrow the read-only expected manifest; use build_execution for detached reports."""
    return observe_execution(region, code, machine)["manifest"]


def execution_context(
    region: Any, code: Any, machine: Any, admission_epoch: int | None = None
) -> dict:
    """Overlay only changes this active job crossed, not another job's earlier history."""
    manifest = current_execution(region, code, machine)
    if admission_epoch is not None and admission_epoch != manifest["epoch"]:
        changes = [
            copy.deepcopy(change)
            for change in machine._execution_transitions.get(region.index, [])
            if change["epoch"] > admission_epoch
        ]
        if changes:
            manifest = {
                **manifest,
                "completeness": {
                    **manifest["completeness"],
                    "transitions": changes,
                    "complete": False,
                },
            }
    return manifest


def reuse_decision(region: Any, code: Any, machine: Any, mode: str | None = None) -> dict:
    """Borrow cached SELF validation, never validation of a persisted or admission candidate.

    Result consumers must first compare their trusted pinned admission hash AND epoch.
    This helper alone does not authorize reuse across a job's configuration transitions.
    """
    mode = machine.persistence_compatibility if mode is None else mode
    current = current_execution(region, code, machine)
    if mode not in MODES:
        return compatibility(current, current, mode)
    return region.execution_decisions[mode]


def execution_for_job(job: Any, code: Any, machine: Any) -> dict:
    """Python-job convenience wrapper; native callers use Entry.region and execution_context."""
    return execution_context(job.entry.region, code, machine, job.stats.get("execution_epoch"))


def note_execution_change(machine: Any, lane: Any, kind: str) -> None:
    """After a successful lane mutation, observe affected regions without blind epoch bumps.

    ``kind`` describes the caller's event; observed differences determine the markers.
    A setter must also run for a change back, since polling cannot detect transient edits.
    """
    for region in machine.code.regions:
        if id(lane) in _static_execution(region, machine.code)["lanes"]:
            observe_execution(region, machine.code, machine)


def _decision(status: str, allow: bool, code: str = "", path: str = "", message: str = "") -> dict:
    return {
        "status": status,
        "allow": allow,
        "issues": [] if not code else [{"code": code, "path": path, "message": message}],
    }


def compatibility(saved: Any, current: Any, mode: str) -> dict:
    """Expected incompatibilities are data. Unknown equality never certifies persistence."""
    if mode not in MODES:
        return _decision("invalid", False, "invalid_mode", "mode", "expected warn or strict")
    for label, manifest in (("saved", saved), ("current", current)):
        if type(manifest) is not dict or manifest.get("schema") != SCHEMA:
            return _decision(
                "invalid", False, "invalid_manifest", label, "missing execution manifest"
            )
        if type(manifest.get("version")) is not int or manifest["version"] != VERSION:
            return _decision(
                "unsupported_version",
                False,
                "unsupported_version",
                label,
                "unsupported execution manifest version",
            )
        try:
            identity = manifest["identity"]
            completeness = manifest["completeness"]
            valid = (
                type(identity) is dict
                and set(identity) == {"program", "layout", "calls", "runtime_policy"}
                and type(completeness) is dict
                and type(completeness["complete"]) is bool
                and type(completeness["unknown"]) is list
                and type(completeness["transitions"]) is list
                and completeness["complete"]
                == (not completeness["unknown"] and not completeness["transitions"])
                and all(
                    type(x) is dict and type(x.get("path")) is str and type(x.get("reason")) is str
                    for x in completeness["unknown"]
                )
                and manifest["hash"]
                == _hash({"schema": SCHEMA, "version": VERSION, "identity": identity})
            )
        except (KeyError, TypeError, ValueError, RecursionError):
            valid = False
        if not valid:
            return _decision(
                "invalid", False, "invalid_manifest", label, "malformed execution manifest or hash"
            )
    if saved["completeness"]["transitions"] or current["completeness"]["transitions"]:
        return _decision(
            "incompatible",
            False,
            "unsupported_history",
            "completeness.transitions",
            "mixed execution configuration history is not resumable",
        )
    if saved["hash"] != current["hash"]:
        return _decision(
            "incompatible",
            False,
            "identity_mismatch",
            "identity",
            "execution, signature, policy or code layout differs",
        )
    unknown = saved["completeness"]["unknown"] + current["completeness"]["unknown"]
    if unknown:
        decision = _decision("unverified", mode == "warn")
        decision["issues"] = [
            {"code": "identity_incomplete", "path": path, "message": reason}
            for path, reason in sorted({(x["path"], x["reason"]) for x in unknown})
        ]
        return decision
    return _decision("compatible", True)


def binding_fingerprint(bind: dict, defaults: Any) -> str:
    """IN-PROCESS mutation guard, not a portable execution hash. Object IDs never persist."""
    rows = []
    for pattern, binding in sorted(bind.items()):
        row = {"pattern": pattern, "object": id(binding), "type": _label(binding)}
        if isinstance(binding, (Use, Inline)):
            for field in (
                "implementation_revision",
                "binding_revision",
                "warn_us",
                "store",
                "idempotent",
                "deterministic",
            ):
                row[field] = _literal(getattr(binding, field, None), [], field)
            if isinstance(binding, Use):
                row.update(
                    target=id(binding.target),
                    adapter=id(binding.adapter),
                    policy=_literal(getattr(binding.policy, "timeout_s", None), [], "policy"),
                    explicit_policy=binding.policy is not None,
                )
        rows.append(row)
    return _hash({"bindings": rows, "adapter": id(defaults.adapter), "protocol": defaults.protocol})


def validate_bindings(machine: Any) -> None:
    """Refuse unsupported rebinding before a load can reuse immutable Bound instructions."""
    expected = machine._binding_fingerprint
    if binding_fingerprint(machine.bind, machine.defaults) != expected:
        raise ConstructionError(
            [
                Problem(
                    "bind",
                    "machine bindings changed after construction; declare all bindings upfront "
                    "(unused bindings start lazily on load), or construct a new Machine",
                )
            ]
        )


def warn_unverified(machine: Any, decision: dict, *, execution_hash: str) -> None:
    """Persistence consumers call this on accepted unverified reuse; ordinary runs stay quiet."""
    if decision["status"] != "unverified" or not decision["allow"]:
        return
    warned = machine._persistence_warnings
    key = ("unverified", execution_hash)
    if key in warned:
        return
    warned.add(key)
    issues = decision["issues"]
    first = issues[0] if issues else {"path": "execution", "message": "identity incomplete"}
    log.warning(
        "persisted execution is unverified (%d identity gaps): %s: %s",
        len(issues),
        first["path"],
        first["message"],
    )
