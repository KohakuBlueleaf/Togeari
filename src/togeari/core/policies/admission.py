"""Admission: how many jobs are resident (spec/05 §5.1).

``allows(pool, signals)``: ``pool`` is a ``PoolView`` (``jobs``, ``bytes``);
``signals`` (built by the dispatcher each check) holds ``jobs``, ``bytes``,
``window``, ``lanes`` {name: {queue, inflight, units_up, waiting}}, ``units``
{name: Load}.
"""

from collections.abc import Mapping
from typing import Any

from togeari.core.contracts.views import PoolView


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


class All:
    def __init__(self, *rules: Any):
        self.rules = rules

    def allows(self, pool: PoolView, signals: Mapping) -> bool:
        return all(r.allows(pool, signals) for r in self.rules)
