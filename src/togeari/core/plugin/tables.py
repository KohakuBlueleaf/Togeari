"""Core queries and commands, answered / applied by the dispatcher between cycles (spec/10 §10.4).

Queries answer copies. Commands that need work off the dispatcher thread
(starting units) return a Future; the reply follows it.
"""

import copy
import reprlib
from typing import Any

from togeari.core.construct.knobs import checked
from togeari.core.program.execution import current_execution, observe_execution

# Input reprs of the `job` query: bounded in size and depth, whatever the value
_repr = reprlib.Repr()
_repr.maxstring = 200
_repr.maxother = 200
_repr.maxlong = 200


def _span(m: Any, addr: int) -> str:
    ins = m.code.mem[addr]
    return str(ins.span) if ins.span else f"@{addr} {ins.op}"


def _job(m: Any, id: int):
    j = m.dispatcher.jobs.get(int(id))
    if j is None:
        raise KeyError(f"no resident job {id}")
    return j


# ---------------------------------------------------------------- queries
def q_jobs(m: Any) -> list[dict]:
    return [
        {
            "id": j.id,
            "key": j.key,
            "entry": j.entry.name,
            "strands": len(j.strands),
            "window": len(j.window),
            "unfinished": j.unfinished,
            "paused": j.paused,
        }
        for j in m.dispatcher.jobs.values()
    ]


def q_job(m: Any, id: int) -> dict:
    j = _job(m, id)
    strands = [
        {
            "id": s.id,
            "state": s.state,
            "pc": s.pc,
            "at": _span(m, s.pc),
            "origin": m.code.mem[s.pc].origin_dict(),
            "key": s.next_key(),
            "spawner": s.spawn is not None,
        }
        for s in j.strands.values()
    ]
    window = [
        {
            "node": x.instr.node or x.instr.op,
            "at": _span(m, x.instr.addr),
            "origin": x.instr.origin_dict(),
            "key": x.key,
            "state": x.state,
            "unit": x.item.ext.get("unit") if x.item is not None else None,
        }
        for x in j.window
    ]

    # Slots bound to a name in any live strand, each counted once
    ready, pending = j.slot_counts()
    return {
        "id": j.id,
        "key": j.key,
        "entry": j.entry.name,
        "inputs": {k: _repr.repr(v) for k, v in j.inputs.items()},
        "strands": strands,
        "window": window,
        "slots": {"ready": ready, "pending": pending},
        "stats": copy.deepcopy(j.stats),
    }


def _lane_row(lane: Any) -> dict:
    return {
        "name": lane.name,
        "kind": lane.kind,
        **lane.signals(),
        "stats": dict(lane.stats),
        "batch_sizes": dict(lane.batch_sizes),
        "units": [
            {
                "name": st.unit.name,
                "state": st.state,
                "inflight": st.inflight,
                "rate": st.rate,
                "done": st.items_done,
                "errors": dict(st.errors),
            }
            for st in lane.state.values()
        ],
    }


def q_lanes(m: Any) -> list[dict]:
    return [_lane_row(lane) for lane in m.lanes]


def q_units(m: Any) -> list[dict]:
    return [u for lane in q_lanes(m) for u in lane["units"]]


def bound_calls(code: Any, region: Any) -> list[dict]:
    """Detached, explanatory placement/policy rows; not an execution identity.

    Report the resolved per-call policy, not a merged lane deadline: call timeouts
    and lane timeouts have different clock origins and are enforced together at runtime.
    """
    rows = []
    for ins in code.calls(region):
        bound = ins.binding
        if bound is None:
            continue
        rows.append(
            {
                "addr": ins.addr,
                "node": ins.node,
                "origin": copy.deepcopy(ins.origin_dict()),
                "pattern": bound.pattern,
                "inline": bound.inline,
                "lane": None if bound.inline else bound.lane.name,
                "execution_policy": {
                    "source": bound.policy_source,
                    "timeout_s": bound.policy.timeout_s,
                },
            }
        )
    return rows


def q_programs(m: Any) -> list[dict]:
    return [
        {
            "region": r.index,
            "source": r.job.source,
            "entry": r.job.entry,
            "hash": r.semantic_hash[:16],
            "addrs": [r.start, r.end],
            "inputs": [p.name for p in r.job.params],
            "outputs": r.job.outputs,
            "dependencies": copy.deepcopy(r.job.dependencies),
            "calls": bound_calls(m.code, r),
            "execution": copy.deepcopy(current_execution(r, m.code, m)),
            "origins": {
                str(addr): m.code.mem[addr].origin_dict()
                for addr in range(r.start, r.end)
                if m.code.mem[addr].span is not None
            },
        }
        for r in m.code.regions
    ]


def q_signals(m: Any) -> dict:
    return copy.deepcopy(m.dispatcher.signals())


def q_plugins(m: Any) -> list[dict]:
    return [
        {
            "name": p.name,
            "priority": p.priority,
            "enabled": p.name in m.plugins.enabled,
            "errors": m.plugins.errors.get(p.name, 0),
            "hook_s": dict(m.plugins.timing.get(p.name, {})),
        }
        for p in m.plugins.plugins
    ]


def _knob_value(v: Any) -> Any:
    """A knob as the `knobs` query shows it: scalars as they are, tuples and lists as lists,
    anything else by its type name."""
    match v:
        case int() | float() | str() | bool() | None:
            return v
        case tuple() | list():
            return [_knob_value(x) for x in v]
        case _:
            return type(v).__name__


def q_knobs(m: Any) -> dict:
    return {k: _knob_value(v) for k, v in m.knobs.items()}


def q_stats(m: Any) -> dict:
    s = copy.deepcopy({k: v for k, v in m.dispatcher.stats.items() if k != "inline_s"})
    s["inline_s"] = m.dispatcher.inline_totals()
    return s


QUERIES = {
    "jobs": q_jobs,
    "job": q_job,
    "lanes": q_lanes,
    "units": q_units,
    "programs": q_programs,
    "signals": q_signals,
    "plugins": q_plugins,
    "knobs": q_knobs,
    "stats": q_stats,
}


# ---------------------------------------------------------------- commands
def c_attach(m: Any, lane: str, unit: Any):
    return m.attach_now(lane, unit)


def c_detach(m: Any, unit: str, drain: bool = True) -> int:
    return m.detach_now(unit, drain)


def c_cancel_job(m: Any, id: int) -> bool:
    d = m.dispatcher
    j = d.jobs.get(int(id))
    if j is None:
        return False
    d.cancel_job(j, "cancelled by command")
    return True


def c_pause_job(m: Any, id: int) -> bool:
    d = m.dispatcher
    j = d.jobs.get(int(id))
    return j is not None and d.pause_job(j)


def c_resume_job(m: Any, id: int) -> bool:
    d = m.dispatcher
    j = d.jobs.get(int(id))
    return j is not None and d.resume_job(j)


def c_set_knob(m: Any, name: str, value: Any) -> Any:
    if name not in m.knobs:
        raise KeyError(f"no knob {name!r}")
    value, problem = checked(name, value, m.contributed_policies)
    if problem is not None:
        raise ValueError(problem)
    if name == "snapshot_every" and value and m.store is None:
        raise ValueError("snapshot_every needs a machine store (store=)")
    old = m.knobs[name]
    # Only step_budget participates in this execution-manifest version. Observe
    # both sides so a supported change-and-revert cannot disappear between checks.
    if name == "step_budget":
        for region in m.code.regions:
            observe_execution(region, m.code, m)
    m.knobs[name] = value
    if name == "step_budget":
        for region in m.code.regions:
            observe_execution(region, m.code, m)
    return old


LANE_POLICIES = ("former", "select")


def c_set_lane_policy(m: Any, lane: str, policy: str, value: Any) -> str:
    """Swaps a lane's former or selector; a contributed policy may be named. Answers the type
    name of the policy replaced."""
    target = next((x for x in m.lanes if x.name == lane), None)
    if target is None:
        raise KeyError(f"no lane {lane!r}")
    if policy not in LANE_POLICIES:
        raise ValueError(f"policy must be one of {LANE_POLICIES}, not {policy!r}")

    # A name resolves to a contributed policy
    if isinstance(value, str):
        if value not in m.contributed_policies:
            raise ValueError(f"{policy}: no contributed policy {value!r}")
        value = m.contributed_policies[value]

    # A former exactly on batch lanes; the lane checks the contract and keeps the old one on refusal
    match policy:
        case "former" if (value is None) == (target.kind == "batch"):
            raise ValueError(f"lane {lane!r}: a former is set exactly on batch lanes")
        case "select" if value is None:
            raise ValueError(f"lane {lane!r}: select must be a selector")
    old = getattr(target, policy)
    try:
        setattr(target, policy, value)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    return type(old).__name__


def c_load(m: Any, program: Any, entries: list):
    return m.load_now(program, entries)


def c_plugin_add(m: Any, plugin: Any) -> str:
    m.plugins.add(plugin)
    if not m.plugins.load(plugin):
        raise RuntimeError(f"plugin {plugin.name!r}: on_load failed")
    return plugin.name


def c_plugin_enable(m: Any, name: str) -> bool:
    return m.plugins.enable(name)


def c_plugin_disable(m: Any, name: str) -> bool:
    return m.plugins.disable(name)


COMMANDS = {
    "attach": c_attach,
    "detach": c_detach,
    "cancel_job": c_cancel_job,
    "pause_job": c_pause_job,
    "resume_job": c_resume_job,
    "set_knob": c_set_knob,
    "set_lane_policy": c_set_lane_policy,
    "load": c_load,
    "plugin_add": c_plugin_add,
    "plugin_enable": c_plugin_enable,
    "plugin_disable": c_plugin_disable,
}
