"""Shared resource estimates/ledger for Python and native lanes; no physical reservations."""

import sys
from fractions import Fraction

from togeari.core.contracts.resources import Estimate, EstimateError, ResourceBudgetExceeded
from togeari.core.contracts.results import ItemError
from togeari.core.lanes.deadline import current
from togeari.core.policies.resources import resource_former

_MAX_RESOURCE = Fraction(sys.float_info.max)


def _reported(value):
    return sys.float_info.max if value > _MAX_RESOURCE else float(value)


PHASES = ("queued", "retry_held", "inflight")


class Candidates:
    """A former view whose bounded candidates have already passed selector admission."""

    def __init__(self, bucket, candidates):
        self.bucket = bucket
        self._candidates = candidates
        self.observed = []

    def __len__(self):
        return len(self.bucket)

    def __getattr__(self, name):
        return getattr(self.bucket, name)

    def candidates(self, limit):
        self.observed = (
            self._candidates(limit) if callable(self._candidates) else self._candidates[:limit]
        )
        return self.observed


class Resources:
    def __init__(self, policy):
        self.policy = policy
        self.entries = {}
        self.unknown = set()
        self.totals = {phase: {name: Fraction(0) for name in policy.budgets} for phase in PHASES}
        self.pending = []
        self.rejected = 0
        self.oversized_batches = 0

    def move(self, item, phase, estimate=None):
        if phase == "inflight" or phase is None:
            item._resource_wait = None
        self.unknown.discard(id(item))
        previous = self.entries.pop(id(item), None)
        if previous is not None:
            _, old_phase, old_estimate = previous
            for name, value in old_estimate.costs:
                self.totals[old_phase][name] -= Fraction(value)
            if estimate is None:
                estimate = old_estimate
        if phase is not None and estimate is not None:
            self.entries[id(item)] = (item, phase, estimate)
            for name, value in estimate.costs:
                self.totals[phase][name] = self.totals[phase].get(name, Fraction(0)) + Fraction(
                    value
                )

    def reject(self, item, result):
        self.move(item, None)
        if item.ext.get("settled") or item.ext.get("squashed"):
            return
        error = result.error
        item.ext["policy_failure"] = {
            "reason": (
                "resource_budget"
                if isinstance(error, ResourceBudgetExceeded)
                else "resource_estimate"
            ),
            "scope": "call",
            "source": "former",
            "budgets": dict(self.policy.budgets) if self.policy is not None else {},
        }
        if isinstance(error, ResourceBudgetExceeded):
            item.ext["policy_failure"].update(
                costs=dict(error.costs),
                oversized_dimensions=list(error.oversized_dimensions),
            )
        item.ext["settled"] = True
        self.pending.append((item, result))
        self.rejected += 1

    def prepare(self, item, phase="queued", force=False):
        if self.policy is None:
            self.move(item, None)
            return True
        try:
            result = self.policy.prepare(item, force=force)
        except Exception:  # noqa: BLE001
            result = ItemError(EstimateError("resource preflight failed"))
        if type(result) is not Estimate:
            if not isinstance(result, ItemError):
                result = ItemError(EstimateError("resource preflight returned an invalid estimate"))
            self.reject(item, result)
            return False
        self.move(item, phase, result)
        return True

    def snapshot(self):
        phases = {
            phase: {name: _reported(value) for name, value in costs.items()}
            for phase, costs in self.totals.items()
        }
        totals = {}
        overflow = set()
        for costs in self.totals.values():
            for name, value in costs.items():
                totals[name] = totals.get(name, Fraction(0)) + value
                if value > _MAX_RESOURCE:
                    overflow.add(name)
        overflow.update(name for name, value in totals.items() if value > _MAX_RESOURCE)
        return {
            **phases,
            "total": {name: _reported(value) for name, value in totals.items()},
            "unknown_items": len(self.unknown),
            "overflow_dimensions": sorted(overflow),
            "rejected": self.rejected,
            "oversized_batches": self.oversized_batches,
        }

    def view(self, item=None):
        if item is None:
            return {"schema": 1, **self.snapshot()}
        entry = self.entries.get(id(item))
        if entry is None:
            return {"schema": 1, "known": False}
        _, phase, estimate = entry
        return {
            "schema": 1,
            "known": True,
            "phase": phase,
            "costs": estimate.as_dict(),
            "oversized": estimate.oversized,
            "wait_reason": dict(item._resource_wait) if item._resource_wait else None,
        }

    def drain(self):
        pending, self.pending = self.pending, []
        return pending


def note_selection(items, chosen):
    selected = {id(item) for item in chosen}
    for item in items:
        item._resource_wait = None if id(item) in selected else {"reason": "budget_prefix"}


def reject_selection(resource, items):
    for item in items:
        resource.reject(item, ItemError(EstimateError("resource selection failed")))


def refresh(lane, former):
    resource = configure(lane, former)
    if resource is None:
        return
    # Only configuration swaps scan: steady-state enqueue/take use cached per-item entries.
    queued = lane.queue
    held = lane._resource_held()
    known = {id(item) for item in queued + held}
    waiting = [(item, "queued") for item in queued] + [(item, "retry_held") for item in held]
    waiting += [
        (item, phase)
        for item, phase, _ in list(resource.entries.values())
        if phase != "inflight" and id(item) not in known
    ]
    for item, phase in waiting:
        if not resource.prepare(item, phase):
            lane.remove(item)


def configure(lane, former):
    policy = resource_former(former)
    resource = getattr(lane, "_resources", None)
    if resource is None and policy is not None:
        resource = lane._resources = Resources(policy)
        # A newly enabled budget cannot retroactively estimate already submitted work.
        resource.unknown.update(
            id(item) for sub in getattr(lane, "subs", {}).values() for item in sub.items
        )
    if resource is not None:
        resource.policy = policy
        if policy is not None:
            for costs in resource.totals.values():
                for name in policy.budgets:
                    costs.setdefault(name, Fraction(0))
    return resource


def transformed(hook):
    if hook is None:
        return False
    owner = getattr(hook, "__self__", None)
    plugins = getattr(owner, "plugins", None)
    return plugins.has("pre_submit") if plugins is not None else True


def post_transform(lane, original, items, unit, now):
    """Force cheap post-hook estimates, repack a safe prefix and requeue every extra."""
    resource = lane._resources
    policy = resource.policy
    orders = {id(current(item)): item._resource_order for item in original}
    for item in original:
        resource.move(item, None)
    if policy is None:
        return items
    eligible, extras = [], []
    key = None
    closed = False
    force = transformed(lane.hook_submit)
    for item in items:
        if id(item) in orders:
            item._resource_order = orders[id(item)]
        if not resource.prepare(item, force=force):
            continue
        try:
            item_key = lane.former.key(item, unit)
        except Exception:  # noqa: BLE001
            resource.reject(item, ItemError(EstimateError("resource compatibility key failed")))
            continue
        if not closed and (not eligible or item_key == key):
            eligible.append(item)
            key = item_key
        else:
            closed = True
            extras.append(item)
    chosen = policy.select(eligible, min(policy.size, max(1, getattr(unit, "max_batch", 1))))
    extras = eligible[len(chosen) :] + extras
    if chosen and resource.entries[id(chosen[0])][2].oversized:
        resource.oversized_batches += 1
    # Original enqueue times remain intact; front-requeue preserves the selected order.
    for item in reversed(extras):
        lane._resource_requeue(item, now)
    return chosen
