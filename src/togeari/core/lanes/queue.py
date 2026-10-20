"""Lane queues: items in priority order, grouped into batch buckets, with former deadlines.

A ``Queue`` keeps its items in ``Bucket``s keyed by ``former.key(item, unit)`` (one
bucket without a former). A bucket is ripe when ``former.deadline(bucket, now) <= now``
(always, without a former). Ripe buckets wait in a heap ordered by their head item;
future deadlines wait in a time heap. Push, remove and take cost O(log n) per item;
heap entries left behind by changes are skipped when they reach the top.
"""

import heapq
import itertools
from collections.abc import Callable
from typing import Any

from togeari.core.contracts.policies import READY

# Order key of an item put back at the front of its queue.
FRONT = -(1 << 62)

# Heap size over live entries at which a heap is rebuilt.
_SLACK = 64


class Entry:
    """One queued item: its order key ``(-priority, seq)``, bucket and whether it is queued."""

    __slots__ = ("order", "item", "bucket", "live")

    def __init__(self, order: tuple, item: Any, bucket: "Bucket"):
        self.order = order
        self.item = item
        self.bucket = bucket
        self.live = True


class Bucket:
    """The queued items of one batch key, as a heap in queue order plus the summaries
    formers read: ``len``, ``due``, ``cap``, ``head()``, ``items()``."""

    __slots__ = ("key", "queue", "seq", "heap", "dues", "size", "deadline", "version", "ripe_at")

    def __init__(self, key: Any, queue: "Queue", seq: int):
        self.key = key
        self.queue = queue
        self.seq = seq
        self.heap: list = []  # (order, entry)
        self.dues: list = []  # (due, order, entry)
        self.size = 0
        self.deadline: float | None = None
        self.version = 0
        # Head order of the bucket's current ripe-heap entry; None while not ripe
        self.ripe_at: tuple | None = None

    def __len__(self) -> int:
        return self.size

    @property
    def cap(self) -> int:
        return self.queue.cap

    @property
    def due(self) -> float | None:
        dues = self.dues
        while dues and not dues[0][2].live:
            heapq.heappop(dues)
        return dues[0][0] if dues else None

    def head(self) -> Entry | None:
        heap = self.heap
        while heap and not heap[0][1].live:
            heapq.heappop(heap)
        return heap[0][1] if heap else None

    def items(self) -> list:
        return [e.item for _, e in sorted(self.heap) if e.live]

    def compact(self) -> None:
        if len(self.heap) > 2 * self.size + _SLACK:
            self.heap = [x for x in self.heap if x[1].live]
            heapq.heapify(self.heap)
        if len(self.dues) > 2 * self.size + _SLACK:
            self.dues = [x for x in self.dues if x[2].live]
            heapq.heapify(self.dues)


class Queue:
    """One lane queue: the shared one, or one unit's under early forming.

    ``key_unit`` is the unit whose ``key()`` the former buckets by; ``cap`` the smallest
    ``max_batch`` among the lane's units. A former method that raises is reported to
    ``failed(what, exc)`` and replaced for that call: ``key`` by None, ``due`` by None,
    ``deadline`` by ``READY``.
    """

    def __init__(
        self,
        former: Any,
        key_unit: Any,
        cap: int,
        failed: Callable[[str, BaseException], None] | None = None,
    ):
        self.former = former
        self.key_unit = key_unit
        self.cap = cap
        self.failed = failed
        self.buckets: dict = {}
        self.index: dict[int, Entry] = {}
        self.size = 0
        self.ripe: list = []  # (head order, push seq, bucket)
        self.timeline: list = []  # (deadline, bucket seq, version, bucket)
        self._bseq = itertools.count()
        self._rseq = itertools.count()

    def __len__(self) -> int:
        return self.size

    # ------------------------------------------------------------ items in and out
    # ------------------------------------------------------------ former calls
    def _ask(self, what: str, fn: Callable, fallback: Any, *args: Any) -> Any:
        """``fn(*args)``, or ``fallback`` once a raise is reported to ``failed``."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            if self.failed is not None:
                self.failed(f"former {what}", exc)
            return fallback

    def push(self, item: Any, order: tuple, now: float) -> None:
        former = self.former
        key = None if former is None else self._ask("key", former.key, None, item, self.key_unit)
        b = self.buckets.get(key)
        if b is None:
            b = self.buckets[key] = Bucket(key, self, next(self._bseq))

        # Entry into the bucket's order heap and, with a due time, its due heap
        e = Entry(order, item, b)
        heapq.heappush(b.heap, (order, e))
        due = None if former is None else self._ask("due", former.due, None, item)
        if due is not None:
            heapq.heappush(b.dues, (due, order, e))
        self.index[id(item)] = e
        b.size += 1
        self.size += 1
        self._refresh(b, now)

    def remove(self, item: Any, now: float) -> bool:
        e = self.index.pop(id(item), None)
        if e is None:
            return False
        self._drop(e)
        e.bucket.compact()
        self._refresh(e.bucket, now)
        return True

    def drain(self) -> list[tuple[tuple, Any]]:
        """Every queued ``(order, item)`` in queue order; the queue is left empty."""
        out = sorted((e.order, e.item) for e in self.index.values())
        self.buckets.clear()
        self.index.clear()
        self.size = 0
        self.ripe.clear()
        self.timeline.clear()
        return out

    def set_former(self, former: Any, now: float) -> None:
        """Use ``former`` from now on; queued items are bucketed again."""
        items = self.drain()
        self.former = former
        for order, item in items:
            self.push(item, order, now)

    def set_cap(self, cap: int, now: float) -> None:
        self.cap = cap
        for b in list(self.buckets.values()):
            self._refresh(b, now)

    def _drop(self, e: Entry) -> None:
        e.live = False
        e.bucket.size -= 1
        self.size -= 1

    # ------------------------------------------------------------ ripeness
    def _refresh(self, b: Bucket, now: float) -> None:
        """Recompute ``b``'s deadline; a ripe bucket enters the ripe heap, a future one the
        time heap."""
        if b.size == 0:
            if self.buckets.get(b.key) is b:
                del self.buckets[b.key]
            b.ripe_at = None
            b.version += 1
            return
        former = self.former
        d = READY if former is None else self._ask("deadline", former.deadline, READY, b, now)

        # Ripe: an entry under the current head order
        if d is not None and d <= now:
            head = b.head().order
            if b.ripe_at != head:
                b.ripe_at = head
                heapq.heappush(self.ripe, (head, next(self._rseq), b))
            b.deadline = d
            return

        # Not ripe: a time-heap entry when the deadline changed
        b.ripe_at = None
        if d != b.deadline:
            b.deadline = d
            b.version += 1
            if d is not None:
                heapq.heappush(self.timeline, (d, b.seq, b.version, b))
        if len(self.timeline) > 2 * len(self.buckets) + _SLACK:
            self._rebuild_timeline()

    def _rebuild_timeline(self) -> None:
        self.timeline = [
            (b.deadline, b.seq, b.version, b)
            for b in self.buckets.values()
            if b.ripe_at is None and b.deadline is not None
        ]
        heapq.heapify(self.timeline)

    def _timeline_top(self) -> tuple | None:
        tl = self.timeline
        while tl:
            _, _, ver, b = tl[0]
            if ver == b.version and b.ripe_at is None and self.buckets.get(b.key) is b:
                return tl[0]
            heapq.heappop(tl)
        return None

    def promote(self, now: float) -> None:
        """Buckets whose deadline passed become ripe."""
        while (top := self._timeline_top()) is not None and top[0] <= now:
            heapq.heappop(self.timeline)
            top[3].version += 1
            top[3].deadline = None
            self._refresh(top[3], now)

    def next_time(self) -> float | None:
        """The earliest future bucket deadline."""
        top = self._timeline_top()
        return None if top is None else top[0]

    # ------------------------------------------------------------ taking
    def take(self, unit: Any, count: Any, admits: Any, now: float) -> list:
        """Items for ``unit`` from the first ripe bucket (by head order) holding items it
        admits; ``count(bucket, unit, now)`` gives how many (an int) or which (a list)."""
        ripe = self.ripe
        aside = []
        out: list = []
        while ripe:
            head, _, b = ripe[0]
            if b.ripe_at != head or self.buckets.get(b.key) is not b:
                heapq.heappop(ripe)
                continue

            # The bucket's batch, then its ripeness again
            want = count(b, unit, now)
            if want:
                out = self._pop(b, want, unit, admits)
            if out:
                heapq.heappop(ripe)
                b.ripe_at = None
                b.compact()
                self._refresh(b, now)
                break
            aside.append(heapq.heappop(ripe))
        for x in aside:
            heapq.heappush(ripe, x)
        if len(ripe) > 2 * len(self.buckets) + _SLACK:
            self.ripe = [
                x for x in ripe if x[2].ripe_at == x[0] and self.buckets.get(x[2].key) is x[2]
            ]
            heapq.heapify(self.ripe)
        return out

    def _pop(self, b: Bucket, want: Any, unit: Any, admits: Any) -> list:
        """The admitted items ``want`` names: the first ``want`` in queue order, or a list."""
        match want:
            case list():
                entries = [self.index.get(id(it)) for it in want]
                taken = [e for e in entries if e is not None and e.bucket is b]
                taken = [e for e in taken if admits(e.item, unit)]
            case _:
                taken, aside = [], []
                heap = b.heap
                while heap and len(taken) < want:
                    x = heapq.heappop(heap)
                    if not x[1].live:
                        continue
                    if admits(x[1].item, unit):
                        taken.append(x[1])
                    else:
                        aside.append(x)
                for x in aside:
                    heapq.heappush(heap, x)
        for e in taken:
            del self.index[id(e.item)]
            self._drop(e)
        return [e.item for e in taken]
