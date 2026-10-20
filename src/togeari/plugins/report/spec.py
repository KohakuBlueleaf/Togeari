"""A machine's construction, as data: programs, entries, bindings, lanes, units, policies, knobs.

Read on the dispatcher thread or while the dispatcher is not running.
"""

from typing import Any

from togeari.core.plugin.base import PluginContext
from togeari.plugins.common.machine import describe, plain


def binding(b: Any) -> dict:
    return {"type": type(b).__name__, **{k: plain(v) for k, v in vars(b).items()}}


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
    }


def machine_spec(ctx: PluginContext) -> dict:
    code = ctx.code
    built = ctx.construction()
    knobs = ctx.knobs

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
        }
        for r in code.regions
    }

    return {
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
