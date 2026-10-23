"""A machine's construction, as data: the core, programs, entries, bindings, lanes, units,
policies, knobs.

Read on the dispatcher thread or while the dispatcher is not running. Policy
rows explain resolved configuration, not a complete execution identity. Advisory
scheduling preferences can still affect timing; no timing-invariance claim is made.

``bindings`` describes construction configuration. Binding configuration is fixed
for a machine: mutating ``machine.bind`` or a ``Use`` object is not a supported
rebind of already loaded regions. Program ``calls`` report the actual resolved
``Bound`` values, not a reinterpretation of that mutable configuration. Documented
lane-policy commands are separate from program rebinding.
"""

from typing import Any

from togeari.core.contracts.execution import ExecutionPolicy
from togeari.core.plugin.base import PluginContext
from togeari.plugins.common.machine import describe, plain


def binding(b: Any) -> dict:
    values = {k: plain(v) for k, v in vars(b).items()}
    policy = getattr(b, "policy", None)
    if isinstance(policy, ExecutionPolicy):
        # None means unspecified; {timeout_s: None} explicitly disables legacy hints.
        values["policy"] = {"timeout_s": policy.timeout_s}
    return {"type": type(b).__name__, **values}


def lane(view: dict) -> dict:
    """A lane's construction from its context view."""
    config = view["config"]
    return {
        "name": view["name"],
        "kind": view["kind"],
        "units": [u["name"] for u in view["units"]],
        "former": describe(config["former"]),
        "select": describe(config["select"]),
        "forming": config["forming"],
        "max_inflight": config["max_inflight"],
        "retry": describe(config["retry"]),
        "timeout_s": config["timeout_s"],
        "lane_dead_s": config["lane_dead_s"],
        "observable_policy": {
            "timeout_s": config["timeout_s"],
            "retry": describe(config["retry"]),
            "lane_dead_s": config["lane_dead_s"],
        },
        "advisory": {
            "former": describe(config["former"]),
            "select": describe(config["select"]),
            "forming": config["forming"],
            "max_inflight": config["max_inflight"],
        },
    }


def machine_spec(ctx: PluginContext) -> dict:
    code = ctx.code
    built = ctx.construction()
    knobs = ctx.knobs

    # The core query owns access to current effective execution state. Source
    # hashes/provenance and descriptive configuration remain separate fields.
    effective = {row["region"]: row for row in ctx.query_now("programs")}
    # Every loaded program region
    regions = {
        r.index: {
            "source": r.job.source,
            "entry": r.job.entry,
            "semantic_hash": r.job.semantic_hash,
            "full_hash": r.job.full_hash,
            "passes": list(r.job.passes),
            "addrs": [r.start, r.end],
            "inputs": [p.name for p in r.job.params],
            "outputs": r.job.outputs,
            "warnings": list(r.job.warnings),
            "calls": effective[r.index]["calls"],
            "execution": effective[r.index]["execution"],
        }
        for r in code.regions
    }

    return {
        "core": built["core"],
        "programs": regions,
        "entries": {
            name: {"region": e.region.index, "addr": e.addr} for name, e in code.entries.items()
        },
        "default_entry": built["default_entry"],
        "passes": built["passes"],
        "bindings": {pattern: binding(b) for pattern, b in built["bind"].items()},
        "lanes": [lane(view) for view in ctx.lanes()],
        "units": {name: plain(spec) for name, spec in ctx.unit_specs().items()},
        "policies": {
            "admission": describe(knobs.get("admission")),
            "issue": describe(knobs.get("issue")),
            "contributed": built["contributed_policies"],
        },
        "store": describe(built["store"]),
        "plugins": ctx.query_now("plugins"),
        "knobs": ctx.query_now("knobs"),
        "warnings": [str(w) for w in built["warnings"]],
    }
