"""Resource-budget batching with explicit observable rejection policy.

Readiness uses count/wait summaries; selection visits only an ordered eligible prefix.
Lanes must prepare/terminalize items before queueing and revalidate transformed submissions.
No payload preparation, device synchronization, physical reservation or best-fit packing.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from itertools import islice
from types import MappingProxyType
from typing import Any

from togeari.core.contracts.resources import (
    Estimate,
    EstimateError,
    ResourceBudgetExceeded,
    resource_limits,
    resource_number,
)
from togeari.core.contracts.results import ItemError
from togeari.core.policies.former import Bucketed, SizeOrWait, _batch_limit


@dataclass(frozen=True, slots=True, init=False, eq=False)
class BudgetFormer(SizeOrWait):
    """Select a resource-fitting priority/FIFO prefix of selector-eligible candidates.

    ``estimate(item)`` reads cheap existing inputs/metadata and supplies every configured
    dimension. ``oversized='singleton'`` deliberately permits a reported soft overrun.
    The lane owns terminal errors; it must not use generic full-batch exception fallback.
    """

    budgets: Mapping[str, float]
    estimate: Callable
    size: int
    wait: float
    oversized: str
    estimator_revision: str | None
    generation: object
    _limits: tuple[tuple[str, float], ...]

    def __init__(
        self,
        budgets: Mapping[str, Any],
        estimate: Callable,
        size: int = 32,
        wait_ms: float = 5.0,
        oversized: str = "reject",
        estimator_revision: str | None = None,
    ):
        if hasattr(self, "generation"):
            raise TypeError("immutable BudgetFormer cannot be reinitialized")
        limits = resource_limits(budgets, where="budgets")
        if not callable(estimate):
            raise TypeError("estimate must be callable")
        if type(size) is not int or size < 1:
            raise ValueError("size must be a positive integer")
        wait = resource_number(wait_ms, positive=False, where="wait_ms") / 1000.0
        if oversized not in ("reject", "singleton"):
            raise ValueError("oversized must be 'reject' or 'singleton'")
        if estimator_revision is not None and (
            type(estimator_revision) is not str or not estimator_revision
        ):
            raise ValueError("estimator_revision must be a nonempty string or None")
        for name, value in (
            ("budgets", MappingProxyType(dict(limits))),
            ("estimate", estimate),
            ("size", size),
            ("wait", wait),
            ("oversized", oversized),
            ("estimator_revision", estimator_revision),
            ("generation", object()),
            ("_limits", limits),
        ):
            object.__setattr__(self, name, value)

    def prepare(self, item: Any, force: bool = False) -> Estimate | ItemError:
        """Cache one immutable estimate/error per item+generation; force after transforms.

        Retry keeps the cached entry. The private cache is not an ``ext`` override channel.
        Exceptions become terminal result data, never a permissive former fallback.
        """
        cache = getattr(item, "_resource_estimates", None)
        if type(cache) is not dict:
            return ItemError(EstimateError("work item has no private resource-estimate cache"))
        if not force and self.generation in cache:
            return cache[self.generation]
        try:
            raw = self.estimate(item)
            if not isinstance(raw, Mapping):
                raise EstimateError("estimate must return a mapping of resource costs")
            costs = []
            for name, _ in self._limits:
                if name not in raw:
                    raise EstimateError(f"missing configured resource dimension {name!r}")
                costs.append(
                    (name, resource_number(raw[name], positive=False, where=f"estimate.{name}"))
                )
            # Extra dimensions are not charged, but malformed estimates are not silently accepted.
            for name, value in raw.items():
                if type(name) is not str or not name:
                    raise EstimateError("estimate dimensions must be nonempty strings")
                if name not in self.budgets:
                    resource_number(value, positive=False, where=f"estimate.{name}")
            oversized = any(cost > limit for (_, cost), (_, limit) in zip(costs, self._limits))
            captured = Estimate(self.generation, tuple(costs), oversized)
            if oversized and self.oversized == "reject":
                result = ItemError(ResourceBudgetExceeded(estimate=captured, budgets=self._limits))
            else:
                result = captured
        except EstimateError as exc:
            result = ItemError(exc)
        except Exception as exc:  # noqa: BLE001
            # Do not invoke arbitrary exception repr/str while containing an estimator failure.
            result = ItemError(EstimateError(f"resource estimate failed: {type(exc).__name__}"))
        cache[self.generation] = result
        return result

    def take(self, bucket: Any, unit: Any, now: float) -> list:
        """Return exact eligible items, never a count that selector filtering could substitute."""
        limit = SizeOrWait.take(self, bucket, unit, now)
        if not limit:
            return []
        limit = min(limit, _batch_limit(unit, self.size))
        return self.select(bucket.candidates(limit), limit)

    def select(self, candidates: Any, limit: int) -> list:
        """Fit a prepared eligible prefix, also used after plugin transforms before submit.

        This does not invoke estimators or consult readiness. Callers must terminalize failed
        preparation and preserve unselected work; an empty list is never full-batch fallback.
        """
        if type(limit) is not int or limit < 0:
            raise ValueError("selection limit must be a nonnegative integer")
        # Exact sums of normalized binary floats avoid dropping small costs at large scales.
        remaining = [Fraction(cost) for _, cost in self._limits]
        chosen = []
        for item in islice(candidates, min(limit, self.size)):
            estimate = getattr(item, "_resource_estimates", {}).get(self.generation)
            if type(estimate) is not Estimate or estimate.generation is not self.generation:
                return []  # Integration preflight must terminalize this case, not leave it queued.
            if estimate.oversized:
                return [item] if not chosen and self.oversized == "singleton" else chosen
            if tuple(name for name, _ in estimate.costs) != tuple(name for name, _ in self._limits):
                return []
            costs = [Fraction(cost) for _, cost in estimate.costs]
            if len(costs) != len(remaining) or any(
                cost > left for cost, left in zip(costs, remaining)
            ):
                break
            chosen.append(item)
            remaining = [left - cost for left, cost in zip(remaining, costs)]
        return chosen

    def policy_descriptor(self) -> dict:
        """Detached observable policy, excluding generation and performance-only count/wait."""
        return {
            "schema": "togeari.resource_budget",
            "version": 1,
            "budgets": dict(self._limits),
            "oversized": self.oversized,
            "error_contract": "terminal-estimate-or-budget-error/1",
            "estimator_revision": self.estimator_revision,
        }


def resource_former(former: Any) -> BudgetFormer | None:
    """Locate the exact supported budget policy through exact Bucketed composition."""
    seen = set()
    while type(former) is Bucketed:
        if id(former) in seen:
            return None
        seen.add(id(former))
        former = former.inner
    return former if type(former) is BudgetFormer else None


def resource_policy(former: Any) -> dict | None:
    """Static observable descriptor; no estimator/user hook is executed by inspection."""
    budget = resource_former(former)
    return None if budget is None else budget.policy_descriptor()
