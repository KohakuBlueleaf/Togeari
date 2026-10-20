# Lane queues

`Queue` in `src/togeari/core/lanes/queue.py` holds one lane queue: the shared one, or one unit's
under early forming. Items are grouped into `Bucket`s by the former's key, so a former reads
bucket summaries instead of the whole queue. Push, remove and take cost O(log n) per item.

## Objects

| Class | Holds |
|---|---|
| `Entry` | `order` (`(-priority, seq)`, or `(FRONT, -seq)` for a re-queued item), `item`, `bucket`, `live` |
| `Bucket` | `key`; `heap` of `(order, entry)`; `dues` heap of `(due, order, entry)`; `size`; `deadline`; `version`; `ripe_at` (head order of its ripe-heap entry, `None` while not ripe) |
| `Queue` | `former`; `key_unit` (the unit whose `key()` the former buckets by); `cap` (the smallest `max_batch` among the lane's units); `buckets` by key; `index` by `id(item)`; `ripe` heap of `(head order, seq, bucket)`; `timeline` heap of `(deadline, seq, version, bucket)` |

What a former may read of a bucket: `len(bucket)`, `bucket.due` (earliest item due, or `None`),
`bucket.cap`, `bucket.head()`, `bucket.items()` (all items in queue order, O(n log n)).

Removed entries are marked not live and skipped when they reach a heap top; a heap is rebuilt
once its size passes twice the live entries plus 64.

## Ripeness

`_refresh(bucket, now)` runs on every change to a bucket:

- empty: the bucket is dropped;
- `d = READY` without a former, else `former.deadline(bucket, now)`;
- `d <= now` (ripe): an entry in `ripe` under the bucket's current head order;
- otherwise: a `timeline` entry when the deadline changed.

`promote(now)` moves every bucket whose timeline deadline passed into `ripe`. `next_time()` is
the earliest future bucket deadline (for `Lane.next_deadline`).

## Operations

| Method | Does |
|---|---|
| `push(item, order, now)` | bucket `former.key(item, key_unit)` (one bucket without a former); entry into its heap and, with `former.due(item)`, its due heap; `_refresh` |
| `remove(item, now)` | marks the entry dead, `_refresh` |
| `drain()` | every `(order, item)` in queue order; the queue is left empty |
| `set_former(former, now)` | drain and push everything again |
| `set_cap(cap, now)` | refresh every bucket |
| `take(unit, count, admits, now)` | below |

`take` walks ripe buckets by head order. For each, `count(bucket, unit, now)` gives an int (take
the first that many items `admits(item, unit)` accepts, in queue order) or a list (those items,
when admitted). The first bucket that yields items is refreshed and the items returned; buckets
that yielded nothing go back on the ripe heap.

## Example

A batch lane with `SizeOrWait(size=4, wait_ms=5)` whose units have `max_batch >= 4` and a
`key()` that returns the item's shape:

| Enqueue | Bucket | Bucket deadline | Ripe |
|---|---|---|---|
| `a` (shape 1) at t=0 | `1: [a]` | due of `a` = t+5 ms | no (timeline) |
| `b`, `c`, `d` (shape 1) | `1: [a, b, c, d]` | `READY` (full) | yes |
| `e` (shape 2) at t=1 | `2: [e]` | t+6 ms | no |

A pump with a free unit takes `[a, b, c, d]` from bucket `1`. Bucket `2` becomes ripe when
`promote` runs at or after t+6 ms, the time `Lane.next_deadline` reports.
