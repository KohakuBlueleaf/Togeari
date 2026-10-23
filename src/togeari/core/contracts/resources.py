"""Immutable estimates and validation shared by resource policies and lane accounting.

Estimates are additive scheduling costs, not reservations or measurements of physical
memory. A generation belongs to one estimator/configuration instance, not input equality.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real
from types import MappingProxyType
from typing import Any


class EstimateError(ValueError):
    """An estimator failed or did not supply a finite nonnegative configured cost."""


@dataclass(frozen=True, slots=True, init=False)
class Estimate:
    """Validated costs in canonical dimension order, captured for one estimator generation."""

    generation: object
    costs: tuple[tuple[str, float], ...]
    oversized: bool = False

    def __init__(self, generation: object, costs, oversized: bool = False):
        if hasattr(self, "generation"):
            raise TypeError("immutable Estimate cannot be reinitialized")
        if type(oversized) is not bool:
            raise ValueError("oversized must be a bool")
        normalized = []
        seen = set()
        for name, value in costs:
            if type(name) is not str or not name or name in seen:
                raise ValueError("estimate dimensions must be unique nonempty strings")
            seen.add(name)
            normalized.append(
                (name, resource_number(value, positive=False, where=f"estimate.{name}"))
            )
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "costs", tuple(sorted(normalized)))
        object.__setattr__(self, "oversized", oversized)

    def as_dict(self) -> dict[str, float]:
        return dict(self.costs)


class ResourceBudgetExceeded(ValueError):
    """A known estimate exceeds hard limits; diagnostics retain no item/payload data."""

    def __init__(self, message: str | None = None, *, estimate: Estimate | None = None, budgets=()):
        if estimate is not None and type(estimate) is not Estimate:
            raise TypeError("estimate must be an Estimate or None")
        self._estimate = estimate
        limits = dict(budgets)
        self._budgets = resource_limits(limits, where="budgets") if limits else ()
        costs = {} if estimate is None else estimate.as_dict()
        self._oversized_dimensions = tuple(
            name for name, limit in self._budgets if costs.get(name, 0) > limit
        )
        if message is None:
            limits = dict(self._budgets)
            details = ", ".join(
                f"{name}: estimate={costs[name]:g}, budget={limits[name]:g}"
                for name in self._oversized_dimensions
            )
            message = "item exceeds resource budget" + (f" ({details})" if details else "")
        super().__init__(message)

    @property
    def estimate(self) -> Estimate | None:
        return self._estimate

    @property
    def costs(self) -> Mapping[str, float]:
        return MappingProxyType({} if self._estimate is None else self._estimate.as_dict())

    @property
    def budgets(self) -> Mapping[str, float]:
        return MappingProxyType(dict(self._budgets))

    @property
    def oversized_dimensions(self) -> tuple[str, ...]:
        return self._oversized_dimensions


def resource_number(value: Any, *, positive: bool, where: str) -> float:
    """Check the original Real before conversion can round a negative value to -0.0."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(
            f"{where} must be a finite {'positive' if positive else 'nonnegative'} real"
        )
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{where} must be {'positive' if positive else 'nonnegative'}")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{where} must be representable as a finite number") from exc
    if not math.isfinite(normalized) or normalized < 0 or (positive and normalized == 0):
        raise ValueError(f"{where} must be finite and {'positive' if positive else 'nonnegative'}")
    if value > 0 and normalized == 0:
        raise ValueError(f"{where} is too small to represent without losing its positive cost")
    return 0.0 if normalized == 0 else normalized


def resource_limits(values: Mapping, *, where: str) -> tuple[tuple[str, float], ...]:
    """Snapshot a nonempty dimension map into immutable sorted positive finite limits."""
    if not isinstance(values, Mapping) or not values:
        raise ValueError(f"{where} must be a nonempty mapping of resource dimensions")
    if any(type(name) is not str or not name for name in values):
        raise ValueError(f"{where} dimensions must be nonempty strings")
    return tuple(
        (name, resource_number(values[name], positive=True, where=f"{where}.{name}"))
        for name in sorted(values)
    )
