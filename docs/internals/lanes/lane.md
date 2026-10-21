# Lane

`Lane` in `src/togeari/core/lanes/lane.py` is the machine's queue and issue logic for one kind of
engine over one or more interchangeable units. It mixes in `Pump` (`core/lanes/pump.py`) and
`Health` ([unit health](health.md), `core/lanes/health.py`); its queues are `Queue` objects
([lane queues](queue.md), `core/lanes/queue.py`). Every method runs on the dispatcher thread;
engines answer through `done`, which becomes an inbox event.

## Constructor

| Argument | Default | Meaning |
|---|---|---|
| `units` | — | one unit or a list |
| `former` | `None` | batch former; required on batch lanes, rejected on others ([machine](../construct/machine.md)) |
| `select` | `LeastLoaded()` | unit choice ([forming and selection](../policies/forming-and-selection.md)) |
| `forming` | `"late"` | `"late"`: one shared queue; `"early"`: per-unit queues; another value is a `ValueError` (`FORMING`) |
| `max_inflight` | `None` | cap on submitted, unsettled items across the lane |
| `retry` | `Retry(max=3, backoff_ms=200.0)` | re-queues per item after `UnitError`, each after the backoff |
| `timeout_s` | `None` | per item, from submit |
| `lane_dead_s` | `300.0` | how long the lane keeps queued items with no unit up; `None` keeps them |
| `name` | `lane<n>` | |

`kind` is the units' common `kind` (`"call"` when absent), or `"mixed"`. `former` and `select`
are properties: setting one checks it against its contract (`_checked`: every method the
contract class declares must be callable, else `TypeError`); a string passes and is resolved by
the machine, where a resolved policy the lane refuses is a construction problem. `forming` is a
property too, checked against `FORMING` when set. Setting `former` re-buckets every queued item.
On a running machine the `set_lane_policy` command sets `former` or `select` on the dispatcher
thread between cycles ([queries and commands](../plugins/queries-and-commands.md)); a refused
policy leaves the old one set.

## State

| Field | Content |
|---|---|
| `_q` | the shared `Queue` |
| `state` | `{id(unit): UnitState}`: `queue` (the unit's own `Queue`), `state`, `inflight`, `subs` (open submission ids in submit order, a dict), `rate` (items/s EWMA), busy time, `items_done`, `errors` by kind, `next_poll`, `backoff`, `submit_lat` |
| `_held`, `_held_ids` | retries waiting for their backoff: a heap of `(ready at, seq, item)` and the live set |
| `_timeouts` | heap of `(deadline, seq, item, submission or None)` |
| `subs`, `inflight` | open `Submission(id, unit, items, ticket, sent, open)` by id; submitted, unsettled items |
| `stats` | `submitted`, `retries`, `failovers`, `recoveries`, `batches`, `no_unit_s`, `dead_failed` |
| `batch_sizes`, `waits` | batch size histogram; queue waits since the last `take_waits()` |
| `_up`, `up_version` | cached units-up list; a counter bumped on every unit state change |
| `_pump_needed`, `_pumped_at` | something changed since the last pump; the last pump time |
| `_specs`, `_identity` | each unit's `spec()` without `name`; their digest |

| View | Gives |
|---|---|
| `units_up()` | units in state `up` (cached until a state change) |
| `queued_now()` / `queued()` | items in the queues / plus retries waiting for their backoff |
| `signals()` | `queue` (`queued()`), `inflight`, `units_up`, `waiting` (sum of `load().waiting` over units up) |
| `queue` | every queued item in queue order (views and tests) |
| `engine_identity()` | `sha256` of the sorted distinct unit specs without `name` ([content keys](../runtime/back-end/issue-and-writeback.md)); recomputed when a unit joins or leaves |

## Enqueue

`enqueue(item, now)` sets `ext["retries"]` and `ext["enqueued"]`. A `WorkItem.deadline` (from
`@hint timeout_s`) is kept as `ext["deadline_cap"]` and armed in `_timeouts` without a
submission, so it also bounds the time queued. The item is routed with order
`(-priority, seq)`: under early forming with a unit up, to the queue of the first unit in
`select.order(up)` that admits it; otherwise to the shared queue. `requeue_front(item)` routes
with order `(FRONT, -seq)`, ahead of everything queued.

`remove(item)` drops a queued or held item. `cancel(item)` (from a squash) marks
`ext["squashed"]`, removes it, or, when every item of its submission is squashed, calls
`unit.cancel(ticket)`.

## Pump

`pump(now)` (`Pump`):

1. Clears `_pump_needed`, records `_pumped_at`.
2. No unit up: start `no_unit_since` (the dead-lane clock) if it is not running, add the time
   since the last pump to `stats["no_unit_s"]` (`no_unit_counted`), so the count grows while the
   lane stays down, and return. Otherwise add the rest of the no-unit time and stop the clock.
   Nothing queued: return.
3. Early forming: items in the shared queue (queued while no unit was up) are spread to the unit
   queues (`_spread`).
4. For each unit in `select.order(units_up, lane)` with work in its queue (the shared queue under
   late forming): `queue.promote(now)`, `cap = unit.capacity()` read once; while `cap > 0`,
   `under_cap()` and the unit is up: `queue.take(unit, _count, _admits, now)`, `_submit`,
   `cap -= 1`.
5. Early forming: an item of the unit's own queue the unit did not admit while taking moves, with
   its order, to the first other unit of that order that admits it (`_respread`), which takes it
   this pump or the next; an item no other unit admits stays.

`_count(bucket, unit, now)` is how many items one submit carries: 1 for call and stream units;
for batch units `max_batch` without a former, else `former.take(bucket, unit, now)` limited to
`max_batch`. `_admits(item, unit)` is `select.admits(item, unit, lane)`. A capacity slot is one
submit: one item on call and stream units, one batch on batch units.

The selector and former calls of the pump go through `_order`, `_admits` and `_count`: the
selector's order keeps only units up, each once; a call that raises counts
`stats["policy_errors"]` (a log line at the 1st, 2nd, 4th ... error) and falls back to the units
up in their own order, admitting every item, or taking the batch limit, so queued items still
leave the lane. A `take` that names items (a list) gets each queued item of its bucket once:
items named twice, or not queued in that bucket, are skipped and count `policy_errors` too.

`_submit(unit, items, now)`:

1. `hook_submit` (the `pre_submit` plugin hook); blocked items are answered and removed.
2. A `Submission`; unit and lane `inflight`; busy-time start.
3. Per item: `protocol` (the unit's, if it has one), `ext["sub"]`, `ext["unit"]`, `ext["sent"]`,
   the queue wait, `_arm_timeout`.
4. Counters; `unit.submit(items if batch else items[0], done)`; `done` calls
   `on_done(lane, sub, results)`. A `submit` that raises answers every item with `UnitError`.
5. `lane.submit`.

`_arm_timeout` sets the in-flight deadline to the earlier of `sent + timeout_s` and
`ext["deadline_cap"]`, writes it to `item.deadline` and `ext["deadline"]`, and pushes it on
`_timeouts`.

## Results

`results(sub, results, now)` settles a submission once (`sub.open`):

1. `_close(sub)`: release the unit's and lane's `inflight` counts and busy time;
   `_pump_needed`.
2. A result count different from the item count turns every result into `ItemError`.
3. Per item not yet `settled`:
   - `UnitError`: skipped for a squashed item; `ItemError(UnitUnavailable)` for an
     `ordered_once` item or one out of retries; otherwise kept for retry;
   - `Ok`: `items_done`, rate EWMA (`0.8 * old + 0.2 / service time`); `ItemError`:
     `errors[kind]`; both settled and returned.
4. Any `UnitError` puts the unit down first; then the kept items count a retry and wait
   `retry.backoff_ms` (`_hold`; 0 re-queues at the front at once).
5. A draining unit with nothing in flight becomes detached.

It returns `(item, result)` pairs for `Dispatcher.deliver`.

## Deadlines

`next_deadline(now)` is the earliest time the lane has something to do, and a past time only
when an action is possible now:

| Counted | When |
|---|---|
| `next_poll` | units `down` |
| the earliest held retry | always |
| the earliest timeout still applying | always (`_timing`: its submission is open, or the item still waits in the lane) |
| `now` | items queued, a unit up, and `_pump_needed` (an enqueue, a result, a state change since the last pump) |
| future former deadlines (`queue.next_time()`) | items queued and a unit up |
| `unit.wake_at()` past `_pumped_at` | items queued for that unit (shared or its own) and the unit up |
| `no_unit_since + lane_dead_s` | items queued and no unit up |

A bucket that is ripe but that the last pump could not serve therefore does not wake the
dispatcher again until something changes.

## Wakes

Capacity can rise without a completion: a worker replaced, a cancelled item's slot freed. An
engine that offers the optional `add_wake(fn)` calls every added `fn()` then (from any thread);
`EngineBase` keeps the functions and `wake()` calls them
([engine contract](../engines/python.md#wakes)).

| Step | Thread | Does |
|---|---|---|
| `Lane._listen(unit)` | dispatcher | at construction and on `_join`: `unit.add_wake(lane._woken)`, once per unit |
| `Lane._woken()` | engine | `on_wake(lane)`, which the machine wires to `Dispatcher.lane_woke` |
| `Dispatcher.lane_woke(lane)` | engine | posts `("call", lane.capacity_rose)` |
| `Lane.capacity_rose()` | dispatcher | `_pump_needed = True`, so `next_deadline` is now and the next cycle pumps |

`wake_at()` covers capacity that rises at a known time (a rate limit); a wake covers capacity
that rises at an unknown time.

## Wiring and events

`Machine._wire(lane)` sets `on_done` (`Dispatcher.lane_done`), `on_wake`
(`Dispatcher.lane_woke`), `hook_submit` (`Dispatcher.before_submit`), `emit`
(`Machine.lane_event` with the lane's name, so every `lane.*` and `unit.*` event the lane emits
carries `lane`) and `wants` (`Bus.wants`: the per-submit `lane.submit` event is built only when
something receives it).
