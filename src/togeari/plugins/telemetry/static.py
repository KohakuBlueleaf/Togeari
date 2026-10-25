"""What does not change with every record: programs, the name table, series names.

``static.kv`` keys:

- ``program/<start>``: a committed region of code memory: its semantic hash, source, entry
  names, address range, the statement table (address -> op, node, graph path, source line),
  its graphs (``kohakunode.graph.to_graph``, kirgraph 3) in both data styles as it runs (after
  the passes: the entry inlined into ``main``), the source's graphs (``source_graphs``: one per
  ``@def``) and ``origins``: each lowered statement path's KIR origin (definition, node, the
  call sites it was inlined through), which maps a run statement to its source node;
- ``names``: the ring's name table rows ``[kind, id, name, appeared_ns, left_ns]``;
- ``series/<sid>``: a metric series' name and unit;
- ``machine``: how the machine was built (core, knobs, lanes and units, plugins); policies
  (admission, a lane's former and selector) as their type and primitive settings.

Values are plain JSON values (stored as MessagePack).
"""

import json
import logging
from typing import Any

from kohakunode.graph import to_graph

from togeari.plugins.common.machine import PRIMITIVES, describe, dumps

log = logging.getLogger("togeari.telemetry")


def plain_json(v: Any) -> Any:
    """``v`` as plain JSON values (objects by ``repr``)."""
    return json.loads(dumps(v))


def program_record(region: Any, mem: list, entries: list[str]) -> dict:
    """The static record of one committed region (``togeari.core.program.code.Region``)."""
    job = region.job
    stmts = []
    for ins in mem[region.start : region.end]:
        span = ins.span
        stmts.append(
            {
                "addr": ins.addr,
                "op": ins.op,
                "node": ins.node if ins.op == "call" else (span.node if span else ""),
                "path": ins.path,
                "line": span.line if span else None,
                "module": span.source if span else None,
                "def": span.def_of if span else None,
            }
        )
    graphs = {style: _graphs(job.program, job, style) for style in ("variables", "wires")}
    source = {style: _graphs(job.written, job, style) for style in ("variables", "wires")}
    origins = (job.origins or {}).get("lowered", {}).get("statements", {})
    return plain_json(
        {
            "hash": region.semantic_hash,
            "name": job.name,
            "source": job.source,
            "entry": job.entry,
            "entries": entries,
            "start": region.start,
            "end": region.end,
            "entry_addr": region.entry_addr,
            "passes": list(job.passes),
            "statements": stmts,
            "graphs": graphs,
            "source_graphs": source,
            "origins": origins,
        }
    )


def _graphs(program: Any, job: Any, style: str) -> dict:
    """``program``'s graph document, or ``{"error": ...}`` when KIR cannot draw it."""
    try:
        return to_graph(program, job.env, style)
    except Exception as exc:
        log.warning("telemetry: to_graph(%s) of %s failed", style, job.name, exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _setting(v: Any) -> Any:
    """A knob or lane setting: containers and primitives as they are, an object (a policy)
    described by its type and primitive attributes."""
    return v if isinstance(v, (dict, list, tuple, *PRIMITIVES)) else describe(v)


def machine_record(ctx: Any) -> dict:
    """How the machine was built (dispatcher thread, or the machine stopped)."""
    c = ctx.construction()
    lanes = [
        {
            "name": lane["name"],
            "kind": lane["kind"],
            "config": {k: _setting(v) for k, v in lane["config"].items()},
            "units": [u["name"] for u in lane["units"]],
        }
        for lane in ctx.lanes()
    ]
    return plain_json(
        {
            "core": c["core"],
            "default_entry": c["default_entry"],
            "passes": c["passes"],
            "knobs": {k: _setting(v) for k, v in ctx.knobs.items()},
            "lanes": lanes,
            "units": ctx.unit_specs(),
        }
    )
