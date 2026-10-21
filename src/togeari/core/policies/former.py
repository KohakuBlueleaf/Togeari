"""Batch forming for batch lanes (spec/04 §4.4).

Formers read the summaries of the lane's buckets (``Former`` in
``togeari.core.contracts.policies``); every method is O(1) per call.
"""

from collections.abc import Callable, Hashable
from typing import Any

from togeari.core.contracts.policies import READY, Former


def _batch_limit(unit: Any, size: int) -> int:
    return max(1, min(size, getattr(unit, "max_batch", size) or size))


class Greedy(Former):
    """Whatever one bucket holds, up to the unit's ``max_batch``, at once."""

    def deadline(self, bucket: Any, now: float) -> float | None:
        return READY if len(bucket) else None

    def take(self, bucket: Any, unit: Any, now: float) -> int:
        return max(1, getattr(unit, "max_batch", 1) or 1)


class SizeOrWait(Former):
    """A batch at ``size`` items, or once an item of the bucket has waited its limit.

    Buckets are the unit's ``key()``. An item's limit is ``wait_ms``, or its
    ``@hint max_wait_ms`` when smaller. A bucket counts as full at
    ``min(size, bucket.cap)`` items; a full or waited-out bucket gives a unit up to
    ``min(size, unit.max_batch)`` items.
    """

    def __init__(self, size: int = 32, wait_ms: float = 5.0):
        self.size = size
        self.wait = wait_ms / 1000.0

    def _limit(self, item: Any) -> float:
        hint = item.ext.get("hint", {}).get("max_wait_ms")
        return self.wait if hint is None else min(self.wait, float(hint) / 1000.0)

    def due(self, item: Any) -> float | None:
        return item.ext.get("enqueued", 0.0) + self._limit(item)

    def deadline(self, bucket: Any, now: float) -> float | None:
        if not len(bucket):
            return None
        if len(bucket) >= max(1, min(self.size, bucket.cap)):
            return READY
        return bucket.due

    def take(self, bucket: Any, unit: Any, now: float) -> int:
        # Full as ``deadline`` counts it, or waited out: up to the unit's own batch limit
        due = bucket.due
        full = len(bucket) >= max(1, min(self.size, bucket.cap))
        if full or (due is not None and due <= now):
            return _batch_limit(unit, self.size)
        return 0


class Bucketed(Former):
    """Buckets split further by ``key(item)``; ``inner`` forms the batches within each."""

    def __init__(self, inner: Any, key: Callable[[Any], Hashable]):
        self.inner = inner
        self.key_fn = key

    def key(self, item: Any, unit: Any) -> Hashable:
        return (self.inner.key(item, unit), self.key_fn(item))

    def due(self, item: Any) -> float | None:
        return self.inner.due(item)

    def deadline(self, bucket: Any, now: float) -> float | None:
        return self.inner.deadline(bucket, now)

    def take(self, bucket: Any, unit: Any, now: float) -> int | list:
        return self.inner.take(bucket, unit, now)
