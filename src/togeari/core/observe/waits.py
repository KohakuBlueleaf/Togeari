"""Opt-in causal wait snapshots, read between dispatcher cycles.

Providers copy metadata only. They never fetch values, call an engine/policy, run a
scheduler gate, or tidy lazy heaps. The native provider uses the same private DTO;
only the renderer's bounded JSON leaves this module. IDs are local to one machine.
"""

import copy
import hashlib
import inspect
import itertools
import json
import time
import uuid
import weakref

from togeari.core.contracts.resources import Estimate
from togeari.core.lanes.lane import Lane
from togeari.core.lanes.native import NativeLane
from togeari.core.lanes.resources import Resources
from togeari.core.runtime.state import InfState, SlotState, StrandState

DEFAULT_LIMIT = 256
MAX_LIMIT = 10_000
_RUNS = weakref.WeakKeyDictionary()
_STATES = {SlotState.PENDING: "pending", SlotState.READY: "ready", SlotState.SQUASHED: "squashed"}
_LIVE = (InfState.WAITING, InfState.READY, InfState.ISSUED)


class _Budget:
    def __init__(self, limit):
        self.left = limit
        self.truncated = False

    def take(self):
        if self.left <= 0:
            self.truncated = True
            return False
        self.left -= 1
        return True

    def rows(self, rows):
        for row in rows:
            if not self.take():
                break
            yield row


def _effective(slot, key, budget):
    """Bounded equivalent of effective(slot,key), including forwarding/mutation chains."""
    seen = set()
    while slot is not None:
        if not budget.take() or id(slot) in seen:
            budget.truncated = True
            return None
        seen.add(id(slot))
        if slot.forward is not None:
            slot = slot.forward
            continue
        best = None
        for order, version in budget.rows(slot.muts):
            if (
                order < key
                and version.state != SlotState.SQUASHED
                and (best is None or order > best[0])
            ):
                best = (order, version)
        if budget.truncated:
            return None
        if best is None:
            return slot
        slot = best[1]
    return None


def _python_state(machine, job_id, limit):
    """Private DTO consumed by the shared renderer; WorkItem refs never leave it."""
    budget = _Budget(limit)
    jobs = machine.dispatcher.jobs
    if job_id is not None and job_id not in jobs:
        raise KeyError(f"no resident job {job_id}")
    selected = (jobs[job_id],) if job_id is not None else jobs.values()
    result = {"jobs": [], "truncated": False, "unsupported": []}
    for job in budget.rows(selected):
        if job.ended:
            continue
        inflights = list(budget.rows(job.window))
        strands = list(budget.rows(job.strands.values()))
        row = {
            "id": job.id,
            "key": job.key,
            "job_execution_id": job.job_execution_id,
            "window_evidence_complete": len(inflights) == len(job.window)
            and len(strands) == len(job.strands),
            "paused": job.paused,
            "unfinished": job.unfinished,
            "window_size": len(job.window),
            "inflights": [],
            "strands": [],
            "slots": [],
        }
        result["jobs"].append(row)
        slots = {}

        def slot_row(slot, slots=slots):
            if slot is None:
                return None
            if slot.id not in slots:
                if not budget.take():
                    return None
                slots[slot.id] = {
                    "id": slot.id,
                    "state": _STATES[slot.state],
                    "phi": slot.phi,
                    "producers": [],
                    "owners": [],
                }
            return slots[slot.id]

        for inf in inflights:
            entry = {
                "id": inf.id,
                "addr": inf.instr.addr,
                "key": list(inf.key),
                "state": inf.state,
                "invocation_id": inf.invocation_id,
                "attempt_ids": list(budget.rows(inf.attempt_ids)),
                "resolution": inf.resolution,
                "inputs": [],
                "outputs": [],
                "item": inf.item,
                "effect_blockers": [],
                "fault": inf.fault is not None,
            }
            row["inflights"].append(entry)
            for port, slot in budget.rows(inf.inputs):
                target = slot_row(_effective(slot, inf.key, budget))
                if target is not None:
                    entry["inputs"].append({"port": port, "slot": target["id"]})
            output_slots = itertools.chain(
                inf.outs,
                (after for _, after in (inf.versions or {}).values()),
                (
                    (inf.collector.output,)
                    if inf.collector is not None and inf.collect_index is None
                    else ()
                ),
            )
            for slot in budget.rows(output_slots):
                target = slot_row(slot)
                if target is not None:
                    entry["outputs"].append(target["id"])
                    target["producers"].append(inf.id)
            effect = inf.instr.ordered or (
                inf.instr.mutates and machine.knobs.get("mutates") == "wait"
            )
            if inf.state == InfState.READY and effect:
                for other in budget.rows(inflights):
                    live = other.state in _LIVE or (
                        other.state == InfState.DONE and other.fault is not None
                    )
                    if live and other.key < inf.key:
                        entry["effect_blockers"].append(
                            {"kind": "inf", "id": other.id, "key": list(other.key)}
                        )
                for st in budget.rows(strands):
                    if st.state != StrandState.ENDED and st.next_key() < inf.key:
                        entry["effect_blockers"].append(
                            {"kind": "strand", "id": st.id, "key": list(st.next_key())}
                        )

        live_keys = [st.next_key() for st in strands if st.state != StrandState.ENDED]
        open_keys = [
            inf.key
            for inf in inflights
            if inf.state in _LIVE or (inf.state == InfState.DONE and inf.fault is not None)
        ]
        oldest_live = min(live_keys, default=None)
        oldest_open = min(open_keys, default=None)
        caps = job.unfinished >= machine.knobs["window"] or (
            len(job.window) - job.unfinished >= machine.knobs["retire_buffer"]
        )
        for strand in strands:
            ins = machine.code.mem[strand.pc]
            key = strand.next_key()
            entry = {
                "id": strand.id,
                "pc": strand.pc,
                "key": list(key),
                "state": strand.state,
                "spawner": strand.spawn is not None,
                "control_slot": None,
                "window_blocked": bool(
                    strand.state == StrandState.RUNNABLE
                    and caps
                    and row["window_evidence_complete"]
                    and (
                        key != oldest_live
                        or (
                            strand.spawn is not None
                            and oldest_open is not None
                            and oldest_open < key
                        )
                    )
                ),
            }
            row["strands"].append(entry)
            if strand.state in (StrandState.PARKED, StrandState.STALLED):
                operand = ins.operand
                if operand is not None and operand[0] == "n" and strand.names is not None:
                    slot = strand.names.get(operand[1])
                    target = slot_row(_effective(slot, key, budget))
                    if target is not None:
                        entry["control_slot"] = target["id"]
            frames = list(budget.rows(strand.frames))
            joins = [f.join for f in frames if f.join is not None]
            collectors = [f.collector for f in frames if f.collector is not None]
            if strand.spawn is not None:
                joins.extend(j for j in (strand.spawn.last, strand.spawn.prev) if j is not None)
                if strand.spawn.collector is not None:
                    collectors.append(strand.spawn.collector)
            for collector in budget.rows(collectors):
                if collector.active:
                    target = slot_row(collector.output)
                    if target is not None:
                        target["owners"].append(strand.id)
            for join in budget.rows(joins):
                if not join.resolved:
                    for slot in budget.rows(join.phis.values()):
                        target = slot_row(slot)
                        if target is not None:
                            target["owners"].append(strand.id)
        cut = getattr(job, "cut", None)
        row["checkpoint"] = (
            None
            if cut is None
            else {
                "request_id": cut.request_id,
                "phase": cut.phase,
                "frontier": None if cut.frontier is None else list(cut.frontier),
                "held": [inf.id for inf in budget.rows(cut.held)],
            }
        )
        row["slots"] = list(slots.values())
    result["truncated"] = budget.truncated
    return result


class _Graph:
    def __init__(self, limit):
        self.limit = limit
        self.entities = {}
        self.relations = {}
        self.truncated = False
        self.unsupported = set()

    def entity(self, identity, kind, **fields):
        if identity in self.entities:
            return identity
        if len(self.entities) + len(self.relations) >= self.limit - 1:
            self.truncated = True
            return None
        self.entities[identity] = {"id": identity, "kind": kind, **fields}
        return identity

    def edge(self, source, target, reason, detail=None, *, port=None, gate=None):
        if source is None or target is None:
            self.truncated = True
            return
        token = json.dumps([source, target, reason, port, gate], separators=(",", ":"))
        identity = "relation:" + hashlib.sha256(token.encode()).hexdigest()
        if identity in self.relations:
            return
        if len(self.entities) + len(self.relations) >= self.limit - 1:
            self.truncated = True
            return
        self.relations[identity] = {
            "id": identity,
            "from": source,
            "to": target,
            "reason": reason,
            "detail": {} if detail is None else detail,
        }

    def finish(self):
        if self.truncated and len(self.entities) + len(self.relations) < self.limit:
            self.entities["summary:truncated"] = {
                "id": "summary:truncated",
                "kind": "summary",
                "state": "truncated",
                "detail": (
                    "Additional state or relations omitted by limit; "
                    "missing producers are not fabricated."
                ),
            }


def _origin(machine, addr):
    if addr is None or not 0 <= addr < len(machine.code.mem):
        return None
    return copy.deepcopy(machine.code.mem[addr].origin_dict())


def _trusted_lane(lane):
    """Allow core lane layouts/inherited methods, never user inspection callbacks."""
    base = NativeLane if isinstance(lane, NativeLane) else Lane
    if not isinstance(lane, Lane) or type(lane).__getattribute__ is not object.__getattribute__:
        return None
    for name in (
        "state",
        "subs",
        "inflight",
        "max_inflight",
        "name",
        "kind",
        "_q",
        "_held_ids",
        "_held",
        "_resources",
    ):
        if inspect.getattr_static(type(lane), name, None) is not inspect.getattr_static(
            base, name, None
        ):
            return None
    return base


def _lane_wait(machine, inf, now, budget):
    located = _lane_cached_wait(machine, inf, now, budget)
    if located is None:
        return None
    lane, detail = located
    if _trusted_lane(lane) is None:
        return located
    resources = vars(lane).get("_resources")
    if type(resources) is Resources:
        # Cached estimates are not physical reservations or blocking evidence.
        if any(held is inf.get("item") for held, _ in budget.rows(resources.pending)):
            return lane, {"phase": "terminal"}
        entry = resources.entries.get(id(inf.get("item")))
        view = {"schema": 1, "known": False}
        if entry is not None and type(entry[2]) is Estimate:
            item, phase, estimate = entry
            reason = getattr(item, "_resource_wait", None)
            view = {
                "schema": 1,
                "known": True,
                "phase": phase,
                "costs": dict(budget.rows(estimate.costs)),
                "oversized": estimate.oversized,
                "wait_reason": (
                    {"reason": "budget_prefix"} if reason == {"reason": "budget_prefix"} else None
                ),
            }
            if (
                phase == "queued"
                and view["wait_reason"] is not None
                and detail["phase"] not in ("running", "terminal", "retry_backoff")
            ):
                detail["phase"] = "resource_budget"
                detail["subreason"] = "budget_prefix"
        detail["resources"] = view
    return lane, detail


def _lane_cached_wait(machine, inf, now, budget):
    """Only cached lane facts; never execute selector/former/engine callbacks."""
    ins = machine.code.mem[inf["addr"]]
    binding = ins.binding
    lane = None if binding is None else binding.lane
    item = inf.get("item")
    if lane is None or item is None:
        return None
    base = _trusted_lane(lane)
    if base is None:
        return lane, {"phase": "unknown", "unsupported": ["custom_lane_wait_state"]}
    inspector = inspect.getattr_static(base, "_wait_item", None)
    candidate = inspect.getattr_static(lane, "_wait_item", None)
    if candidate is not inspector:
        return lane, {"phase": "unknown", "unsupported": ["custom_lane_wait_state"]}
    if inspector is not None:
        if budget.left <= 0:
            budget.truncated = True
            return lane, {"phase": "unknown", "unsupported": ["lane_evidence_limit"]}
        allowance = budget.left
        detail = inspector(lane, item, now, limit=allowance)
        scanned = detail.pop("scanned", allowance)
        budget.left -= min(allowance, max(0, scanned))
        if detail.get("truncated"):
            budget.truncated = True
        return lane, detail
    if not hasattr(lane, "_q"):
        return lane, {"phase": "unknown", "unsupported": ["native_lane_wait_state"]}
    for sub in budget.rows(lane.subs.values()):
        if any(candidate is item for candidate in budget.rows(sub.items)):
            return lane, {"phase": "running"}
    if id(item) in lane._held_ids:
        for when, _, held in budget.rows(lane._held):
            if held is item and when > now:
                return lane, {"phase": "retry_backoff", "remaining_s": when - now}
    queues = itertools.chain((lane._q,), (state.queue for state in lane.state.values()))
    for queue in budget.rows(queues):
        entry = queue.index.get(id(item))
        if entry is None:
            continue
        if lane.max_inflight is not None and lane.inflight >= lane.max_inflight:
            return lane, {
                "phase": "lane_capacity",
                "subreason": "max_inflight",
                "current": lane.inflight,
                "limit": lane.max_inflight,
            }
        states = list(budget.rows(lane.state.values()))
        if states and all(state.state != "up" for state in states) and not budget.truncated:
            return lane, {"phase": "lane_capacity", "subreason": "no_unit_up"}
        deadline = entry.bucket.deadline
        if lane.kind == "batch" and (deadline is None or deadline > now):
            return lane, {
                "phase": "batch_wait",
                "remaining_s": None if deadline is None else deadline - now,
            }
        return lane, {
            "phase": "unknown",
            "subreason": "queued",
            "unsupported": ["unit_capacity_and_selector_admission"],
        }
    return lane, {"phase": "unknown", "subreason": "issued_item_not_located"}


def _render(machine, dto, limit, now):
    graph = _Graph(limit)
    graph.truncated = bool(dto.get("truncated"))
    graph.unsupported.update(dto.get("unsupported", []))
    budget = _Budget(limit * 4)
    for job in budget.rows(dto["jobs"]):
        jid = job["id"]
        job_ref = graph.entity(
            f"job:{jid}",
            "job",
            state="paused" if job["paused"] else "resident",
            job_execution_id=job.get("job_execution_id"),
        )
        infs, strands, slots = {}, {}, {}
        for inf in budget.rows(job["inflights"]):
            infs[inf["id"]] = graph.entity(
                f"inf:{jid}:{inf['id']}",
                "inflight",
                state=inf["state"],
                addr=inf["addr"],
                region=getattr(machine.code.mem[inf["addr"]], "region", None),
                node=machine.code.mem[inf["addr"]].node,
                key=list(inf["key"]),
                origin=_origin(machine, inf["addr"]),
                invocation_id=inf.get("invocation_id"),
                attempt_ids=list(inf.get("attempt_ids", [])),
                resolution=inf.get("resolution"),
            )
        for strand in budget.rows(job["strands"]):
            strands[strand["id"]] = graph.entity(
                f"strand:{jid}:{strand['id']}",
                "strand",
                state=strand["state"],
                addr=strand["pc"],
                region=getattr(machine.code.mem[strand["pc"]], "region", None),
                key=list(strand["key"]),
                origin=_origin(machine, strand["pc"]),
                phase="not_materialized",
                spawner=strand["spawner"],
            )
        for slot in budget.rows(job["slots"]):
            if slot["state"] != "ready":
                slots[slot["id"]] = graph.entity(
                    f"slot:{jid}:{slot['id']}",
                    "slot",
                    state=slot["state"],
                    phi=slot.get("phi", False),
                    origin=None,
                )
        cut = job.get("checkpoint")
        if "checkpoint" not in job:
            graph.unsupported.add("checkpoint_wait_state")
        cut_ref = None
        if cut is not None:
            cut_ref = graph.entity(
                f"checkpoint:{jid}:{cut['request_id']}",
                "checkpoint",
                state=cut["phase"],
                origin=None,
                frontier=None if cut.get("frontier") is None else list(cut["frontier"]),
            )
        for slot in budget.rows(job["slots"]):
            target = slots.get(slot["id"])
            if target is None:
                continue
            for producer in budget.rows(slot.get("producers", [])):
                graph.edge(
                    target, infs.get(producer), "input", {"subreason": "producer"}, gate="producer"
                )
            for owner in budget.rows(slot.get("owners", [])):
                graph.edge(
                    target,
                    strands.get(owner),
                    "control",
                    {"subreason": "unresolved_join"},
                    gate="join",
                )
            if not slot.get("producers") and not slot.get("owners"):
                graph.edge(
                    target,
                    job_ref,
                    "unknown",
                    {"subreason": "producer_not_materialized_or_unavailable"},
                )
        for inf in budget.rows(job["inflights"]):
            source = infs.get(inf["id"])
            waiting = inf["state"] in ("waiting", "ready", "issued")
            if cut_ref is not None and inf["state"] in ("issued", "done"):
                graph.edge(
                    cut_ref,
                    source,
                    "checkpoint",
                    {"phase": cut["phase"], "subreason": "drain"},
                    gate="drain",
                )
            if not waiting:
                continue
            explained = False
            for port in budget.rows(inf.get("inputs", [])):
                if port["slot"] in slots:
                    graph.edge(
                        source,
                        slots[port["slot"]],
                        "input",
                        {"port": port["port"]},
                        port=port["port"],
                    )
                    explained = True
            for blocker in budget.rows(inf.get("effect_blockers", [])):
                targets = infs if blocker["kind"] == "inf" else strands
                graph.edge(
                    source,
                    targets.get(blocker["id"]),
                    "effect",
                    {"older_key": list(blocker["key"])},
                    gate="older_work",
                )
                explained = True
            if cut is not None and (
                inf["id"] in cut.get("held", [])
                or (
                    inf["state"] == "ready"
                    and (cut.get("frontier") is None or tuple(inf["key"]) > tuple(cut["frontier"]))
                )
            ):
                graph.edge(source, cut_ref, "checkpoint", {"phase": cut["phase"]}, gate="cut")
                explained = True
            if inf["state"] == "issued":
                located = _lane_wait(machine, inf, now, budget)
                if located is not None:
                    lane, detail = located
                    phase = detail["phase"]
                    graph.unsupported.update(detail.get("unsupported", []))
                    if phase in ("running", "terminal"):
                        explained = True
                    else:
                        lane_ref = graph.entity(
                            f"lane:{id(lane)}",
                            "lane",
                            state=phase,
                            name=lane.name if _trusted_lane(lane) is not None else None,
                            origin=None,
                        )
                        reason = (
                            phase
                            if phase
                            in ("lane_capacity", "batch_wait", "retry_backoff", "resource_budget")
                            else "unknown"
                        )
                        graph.edge(
                            source,
                            lane_ref,
                            reason,
                            {k: v for k, v in detail.items() if k != "unsupported"},
                            gate=detail.get("subreason", phase),
                        )
                        explained = True
            if not explained:
                graph.edge(
                    source,
                    job_ref,
                    "unknown",
                    {"subreason": "paused" if job["paused"] else "no_recorded_blocker"},
                )
        for strand in budget.rows(job["strands"]):
            source = strands.get(strand["id"])
            explained = False
            slot = strand.get("control_slot")
            if slot in slots:
                graph.edge(source, slots[slot], "control", {"subreason": "operand"}, gate="operand")
                explained = True
            if (
                cut is not None
                and strand["state"] == "runnable"
                and (cut.get("frontier") is None or tuple(strand["key"]) > tuple(cut["frontier"]))
            ):
                graph.edge(source, cut_ref, "checkpoint", {"phase": cut["phase"]}, gate="cut")
                explained = True
            elif strand["state"] == "runnable" and strand.get("window_blocked"):
                window = graph.entity(
                    f"window:{jid}",
                    "window",
                    state="capped",
                    current=job["window_size"],
                    unfinished=job["unfinished"],
                    origin=None,
                )
                graph.edge(
                    source,
                    window,
                    "window",
                    {
                        "window": machine.knobs["window"],
                        "retire_buffer": machine.knobs["retire_buffer"],
                    },
                    gate="caps",
                )
                for blocker in budget.rows(job["inflights"]):
                    if blocker["state"] in ("waiting", "ready", "issued", "done"):
                        graph.edge(
                            window,
                            infs.get(blocker["id"]),
                            "window",
                            {"subreason": "occupant"},
                            gate="occupant",
                        )
                explained = True
            if not explained and strand["state"] in ("parked", "stalled"):
                graph.edge(
                    source, job_ref, "unknown", {"subreason": "control_evidence_unavailable"}
                )
    graph.truncated |= budget.truncated
    graph.finish()
    return graph


def waits(machine, job=None, limit=DEFAULT_LIMIT):
    """One bounded detached snapshot. Call on the dispatcher thread or while stopped."""
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    if job is not None and (type(job) is not int or job < 0):
        raise ValueError("job must be a resident numeric job id or None")
    if machine not in _RUNS:
        _RUNS[machine] = (uuid.uuid4().hex, itertools.count(1))
    run_id, snapshots = _RUNS[machine]
    snapshot_id = next(snapshots)
    if job is not None and job not in machine.dispatcher.jobs:
        raise KeyError(f"no resident job {job}")
    native = getattr(machine.dispatcher, "_wait_state", None)
    if native is not None:
        dto = native(job, limit * 4)
    elif getattr(machine, "core", "python") == "rust":
        dto = {"jobs": [], "truncated": False, "unsupported": ["native_wait_state"]}
    else:
        dto = _python_state(machine, job, limit * 4)
    now = machine.dispatcher.clock() if hasattr(machine.dispatcher, "clock") else time.monotonic()
    graph = _render(machine, dto, limit, now)
    for entity in graph.entities.values():
        entity["id"] = f"{run_id}:{entity['id']}"
    for relation in graph.relations.values():
        for field in ("id", "from", "to"):
            relation[field] = f"{run_id}:{relation[field]}"
    return {
        "schema": 1,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "job": job,
        "entities": list(graph.entities.values()),
        "relations": list(graph.relations.values()),
        "truncated": graph.truncated,
        "unsupported": sorted(graph.unsupported),
    }
