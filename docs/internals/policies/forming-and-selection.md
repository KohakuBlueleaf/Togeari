# Forming and unit choice

A lane's `former` builds batches for batch units; its `select` decides which units get work first
and which items each unit may take. Contracts: `Former`, `Selector` and `READY` in
`src/togeari/core/contracts/policies.py`; builtins in `src/togeari/core/policies/former.py` and
`src/togeari/core/policies/select.py`. The lane side is in [lane](../lanes/lane.md) and
[lane queues](../lanes/queue.md).

Both contracts carry default method bodies, so a policy may subclass them and write only what
differs. The lane calls exactly the methods the contract declares and checks a policy for them
when it is set.

## Former contract

| Method | Default | Meaning |
|---|---|---|
| `key(item, unit)` | `unit.key(item)`, or `None` when it raises | the bucket; items of one bucket may share a batch |
| `due(item)` | `None` | monotonic time at which the item alone makes its bucket takeable |
| `deadline(bucket, now)` | `READY` for a non-empty bucket (`take` is asked at every pump) | when `take` may succeed by time alone: `READY` (`-inf`) or a past time means now; `None` never; depends only on the bucket's items |
| `take(bucket, unit, now)` | — | how many head items to submit now (0: wait), or a list of the bucket's items |

| Former | `deadline` | `take` |
|---|---|---|
| `Greedy()` | `READY` while the bucket is non-empty | `max_batch` of the unit |
| `SizeOrWait(size=32, wait_ms=5.0)` | `READY` at `min(size, bucket.cap)` items, else `bucket.due` | `min(size, max_batch)` once the bucket is that full or its earliest due passed, else 0 |
| `Bucketed(inner, key)` | `inner.deadline` | `inner.take`; buckets are `(inner.key(item, unit), key(item))` |

`SizeOrWait.due(item)` is `ext["enqueued"]` plus `wait_ms`, or the item's `@hint max_wait_ms`
when smaller.

## Selector contract

| Method | Default | Used by |
|---|---|---|
| `order(units, lane)` | units as given | `Lane.pump` (unit order), early forming (which unit's queue) |
| `admits(item, unit, lane)` | `True` | `Queue.take`, early forming |

Each unit, in the selector's order, takes the items it admits in queue order (priority, then
arrival).

| Builtin | `order` | `admits` |
|---|---|---|
| `LeastLoaded` (default) | most `capacity()`, then lowest `load().waiting` | all |
| `Throughput` | highest `lane.rate(unit)`, then capacity | all |
| `RoundRobin` | rotated by one each call | all |
| `Affinity(key="job")` | `LeastLoaded` | the item's preferred unit, or any unit while that one has no capacity (or no unit is up) |
| `ByHint(fallback=LeastLoaded())` | `fallback.order` | the unit named by `@hint unit=`, or any unit when no unit up has that name |

`Affinity` keys an item by `ext["job_key"]` for `key="job"`, by `key(item)` for a callable, and
by `item.inputs[key]` otherwise. The preferred unit is the rendezvous hash
(`max sha1(f"{key!r}/{unit.name}")`) over the units up, remembered per `(lane, key)` while
`lane.up_version` is unchanged (at most 65 536 entries before the memory is cleared).

```python
from togeari.policies import Selector


class Reserved(Selector):
    """Unit "spare" takes only items with priority above zero."""

    def order(self, units, lane):
        return sorted(units, key=lambda u: -lane.rate(u))

    def admits(self, item, unit, lane):
        return unit.name != "spare" or item.priority > 0
```

Selectors and formers see observed state only (capacity, `load()`, rates, bucket summaries);
they never predict an item's run time.
