"""Policy contracts: admission, issue order, batch forming, unit choice (spec/04, spec/05).

``Former`` and ``Selector`` carry default method bodies: a policy may subclass them and
write only what differs. The lane calls exactly the methods declared here.
"""

from collections.abc import Hashable, Mapping
from typing import Any, Protocol, runtime_checkable

from togeari.core.contracts.views import PoolView, ReadyView

# A former deadline that means "take may succeed now".
READY = float("-inf")


@runtime_checkable
class Admission(Protocol):
    def allows(self, pool: PoolView, signals: Mapping) -> bool:
        """Whether one more job may start; ``pool`` and ``signals`` are read-only."""


@runtime_checkable
class Issue(Protocol):
    def order(self, ready: list[ReadyView]) -> list[ReadyView]:
        """The ready statements in issue order; views left out are issued after the ones
        returned, in their arrival order."""

    def priority(self, st: ReadyView) -> int:
        """Passed to the lane queue and to engines."""


@runtime_checkable
class Former(Protocol):
    """Batch forming over buckets the lane keeps (``togeari.core.lanes.queue.Bucket``).

    A bucket holds the queued items with one ``key``; it offers ``len(bucket)``,
    ``bucket.due`` (the earliest ``due`` of its items, or None), ``bucket.cap`` (the
    smallest ``max_batch`` among the lane's units), ``bucket.head()`` and
    ``bucket.items()`` (every item in queue order, O(n log n)).
    """

    def key(self, item: Any, unit: Any) -> Hashable:
        """The bucket of ``item``; items of one bucket may share a batch."""
        try:
            return unit.key(item)
        except Exception:  # noqa: BLE001
            return None

    def due(self, item: Any) -> float | None:
        """Monotonic time at which ``item`` alone makes its bucket takeable; None if never."""
        return None

    def deadline(self, bucket: Any, now: float) -> float | None:
        """When ``take`` may succeed by time alone (``READY`` or a past time: now); None if never.

        Depends on the bucket's items only, so the lane recomputes it on bucket changes.
        The default is ``READY`` for a non-empty bucket: ``take`` is asked at every pump.
        """
        return READY if len(bucket) else None

    def take(self, bucket: Any, unit: Any, now: float) -> int | list:
        """How many head items of ``bucket`` to submit to ``unit`` now (0: wait), or a list of
        the bucket's items to submit."""


@runtime_checkable
class Selector(Protocol):
    """Unit choice: the order units are offered work, and which items each unit may take.

    Every unit takes the items it admits in queue order (priority, then arrival).
    """

    def order(self, units: list, lane: Any) -> list:
        """Candidate units in the order they are offered work."""
        return list(units)

    def admits(self, item: Any, unit: Any, lane: Any) -> bool:
        """Whether ``unit`` may take ``item``."""
        return True
