"""Admission: how many jobs are resident (spec/05 §5.1).

``allows(pool, signals)``: ``pool`` is a ``PoolView`` (``jobs``, ``bytes``);
``signals`` (built by the dispatcher each check) holds ``jobs``, ``bytes``,
``window``, ``lanes`` {name: {queue, inflight, units_up, waiting}}, ``units``
{name: Load}.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from togeari.core.contracts.resources import resource_limits, resource_number
from togeari.core.contracts.views import PoolView
from togeari.core.policies.resources import resource_former


class Fixed:
    def __init__(self, n: int = 64):
        self.n = n

    def allows(self, pool: PoolView, signals: Mapping) -> bool:
        return pool.jobs < self.n


class TargetLoad:
    """Admit while the lane's units report fewer than ``waiting`` waiting in total.

    Keeps a backend just saturated; ``max_jobs`` caps residency regardless.
    Counts the lane's own queue too, so an engine that reports nothing still paces.
    """

    def __init__(self, lane: Any, waiting: int = 8, max_jobs: int = 4096):
        self.lane = lane
        self.waiting = waiting
        self.max_jobs = max_jobs

    def allows(self, pool: PoolView, signals: Mapping) -> bool:
        if pool.jobs >= self.max_jobs:
            return False
        name = self.lane if isinstance(self.lane, str) else self.lane.name
        info = signals["lanes"].get(name)
        if info is None:
            return True
        return info["waiting"] + info["queue"] < self.waiting


class MemoryBound:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes

    def allows(self, pool: PoolView, signals: Mapping) -> bool:
        return pool.bytes < self.max_bytes


@dataclass(frozen=True, slots=True, init=False, eq=False)
class ResourcePressure:
    """Gate new jobs by lane-wide estimated totals, never gate resident draining work.

    Missing lanes/dimensions and unknown estimates fail closed. ``status`` explains the
    decision without reading engine/device state or predicting a future job reservation.
    """

    lane: Any
    limits: Mapping[str, float]
    max_jobs: int

    def __init__(self, lane: Any, limits: Mapping, max_jobs: int = 4096):
        if hasattr(self, "limits"):
            raise TypeError("immutable ResourcePressure cannot be reinitialized")
        if type(max_jobs) is not int or max_jobs < 1:
            raise ValueError("max_jobs must be a positive integer")
        normalized = resource_limits(limits, where="limits")
        object.__setattr__(self, "lane", lane)
        object.__setattr__(self, "limits", MappingProxyType(dict(normalized)))
        object.__setattr__(self, "max_jobs", max_jobs)

    def status(self, pool: PoolView, signals: Mapping) -> dict:
        if pool.jobs >= self.max_jobs:
            return {"allow": False, "reason": "max_jobs"}
        name = self.lane if isinstance(self.lane, str) else self.lane.name
        info = signals.get("lanes", {}).get(name)
        if not isinstance(info, Mapping):
            return {"allow": False, "reason": "missing_lane", "lane": name}
        resources = info.get("resources")
        if not isinstance(resources, Mapping):
            return {"allow": False, "reason": "missing_resource_estimates", "lane": name}
        unknown = resources.get("unknown_items")
        if type(unknown) is not int or unknown < 0:
            return {"allow": False, "reason": "invalid_unknown_count", "lane": name}
        if unknown:
            return {"allow": False, "reason": "unknown_resource_estimates", "lane": name}
        overflow = resources.get("overflow_dimensions", [])
        if type(overflow) not in (list, tuple) or any(type(name) is not str for name in overflow):
            return {"allow": False, "reason": "invalid_overflow_dimensions", "lane": name}
        for dimension in self.limits:
            if dimension in overflow:
                return {
                    "allow": False,
                    "reason": "resource_limit",
                    "dimension": dimension,
                    "overflow": True,
                }
        totals = resources.get("total")
        if not isinstance(totals, Mapping):
            return {"allow": False, "reason": "missing_resource_totals", "lane": name}
        for dimension, limit in self.limits.items():
            if dimension not in totals:
                return {"allow": False, "reason": "missing_dimension", "dimension": dimension}
            try:
                value = resource_number(totals[dimension], positive=False, where=dimension)
            except (TypeError, ValueError):
                return {"allow": False, "reason": "invalid_resource_total", "dimension": dimension}
            if value >= limit:
                return {"allow": False, "reason": "resource_limit", "dimension": dimension}
        return {"allow": True, "reason": "below_limits"}

    def allows(self, pool: PoolView, signals: Mapping) -> bool:
        return self.status(pool, signals)["allow"]


class All:
    def __init__(self, *rules: Any):
        self.rules = rules

    def allows(self, pool: PoolView, signals: Mapping) -> bool:
        return all(r.allows(pool, signals) for r in self.rules)


def validate_resource_admission(admission: Any, lanes: list) -> list[str]:
    """Validate maintained resource admission rules before a machine starts any unit.

    Traverse only exact All/ResourcePressure instances. Arbitrary custom policies are not
    inspected or executed; missing runtime resource data still fails closed via status().
    """
    problems = []
    pending = [(admission, frozenset())]
    while pending:
        rule, ancestors = pending.pop()
        if type(rule) is All:
            if id(rule) in ancestors:
                problems.append("cyclic All admission composition")
                continue
            path = ancestors | {id(rule)}
            pending.extend((child, path) for child in reversed(rule.rules))
            continue
        if type(rule) is not ResourcePressure:
            continue
        if type(rule.lane) is str:
            matching = [lane for lane in lanes if lane.name == rule.lane]
            label = rule.lane
        else:
            matching = [lane for lane in lanes if lane is rule.lane]
            label = getattr(rule.lane, "name", "<unregistered lane>")
        if len(matching) != 1:
            problem = "missing" if not matching else "ambiguous"
            problems.append(f"ResourcePressure lane {label!r} is {problem} in this machine")
            continue
        budget = resource_former(matching[0].former)
        if budget is None:
            problems.append(f"ResourcePressure lane {label!r} needs a supported BudgetFormer")
            continue
        missing = sorted(set(rule.limits) - set(budget.budgets))
        if missing:
            problems.append(f"ResourcePressure lane {label!r} has unestimated dimensions {missing}")
    return problems
