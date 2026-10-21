# Native lanes

A Rust machine adopts each lane it wires (`UnitStage._wire`, `core/lanes/native.py` `adopt`):
the lane keeps its object, and `lane._native` holds a `togeari._rs.Lane` built from it, with the
lane's own unit specs and engine identity. The queues, unit states, submissions, retries,
timeouts and health run there; the dispatcher core calls it directly, and every other reader
goes through the `Lane` interface the lane's new class keeps.

## Adoption

| Lane class | Becomes |
|---|---|
| `Lane` | `NativeLane` |
| a subclass that overrides none of `NativeLane`'s attributes (it may add its own) | `Native<Name>(NativeLane, <Name>)`, made once per class: `isinstance` and the added attributes hold |
| a subclass that overrides one (a dispatcher method, a view, a setting, an internal) | not adopted: a Python lane, which the Rust core calls through its methods |

## Reading and setting

| Read or set through the lane | Answered by the native lane |
|---|---|
| `enqueue`, `pump`, `timers`, `next_deadline`, `results`, `remove`, `cancel`, `requeue_front` | the dispatcher interface |
| `attach`, `detach`, `unit_down`, `capacity_rose` | elastic units, health, wake-ups |
| `units_up`, `up_version`, `rate`, `queued_now`, `queued`, `load_waiting`, `signals`, `take_waits`, `under_cap`, `engine_identity`, `queue` | views |
| `state` (`UnitView` per unit; `subs` in submit order), `stats`, `batch_sizes`, `inflight`, `subs` (`Submission`s) | counters and unit states |
| `max_inflight`, `timeout_s`, `lane_dead_s`, `name`, `units`, `retry` (set) | forwarded when set; `retry.max` and `retry.backoff_ms` are read when a retry happens |
| `former`, `select`, `forming` (set), `clock` | policy swaps re-bucket the queued items; the clock the trace driver sets |

A method of `DIRECT` (`enqueue`, `pump`, `timers`, `next_deadline`, `results`, `cancel`,
`queued`, `units_up`, `queued_now`, `under_cap`, `load_waiting`, `remove`, `unit_down`,
`requeue_front`, `rate`) set on the instance (`lane.results = …`) is called, until it is deleted,
at every point the Python lane calls it: by the core, and inside the native lane (the pump reads
`units_up()`, `queued_now()` and `under_cap()`, a cancel calls `remove`, a timeout `results`, a
retry `requeue_front`, `Throughput` `rate`, `signals()` its parts). Setting one sets a bit in
the native lane (`set_overridden`); with the bit clear the native lane uses its own state. A
`Lane` internal the native lane runs itself (`_route`, `_submit`, `_poll`, …) raises
`NotImplementedError` on an adopted lane, and the Python lane's state attributes (`_q`,
`_held`, …) are gone from it.

Numbers keep Python's semantics: a setting may be an int or a float (`Retry(max=inf)`,
`SizeOrWait(32.0)`); a unit's `capacity()` is counted down as Python counts it (a float counts
until it is no longer above 0; another object is compared with Python's operators, which may
raise as in the Python lane); a former's `take` may answer a list, an int, a float or a bool.
A unit's `kind`, `max_batch`, `name` and `protocol` are read when the Python lane reads them.
The queues' batch cap is the number Python's `min(max(1, max_batch or 1) for each unit)` gives
(`Bucket.cap` returns that object, an int or a float); SizeOrWait's full test
`len >= max(1, min(size, cap))` and its take use Python's `min` and `max` (the first argument
wins a tie or a NaN comparison).

## Queues

`lanes/queue.rs` ports `core/lanes/queue.py` and calls no Python: items in `(-priority, seq)`
order (priorities compared exactly as Python compares numbers: an int in the i128 range, a
larger int as its sign and magnitude, a float), buckets by the former's key, a ripe heap
by head order and a time heap of future deadlines, entries left behind skipped when they reach a
top, heaps rebuilt past twice their live size plus 64. The lane asks the former for keys, due
times and deadlines and looks bucket keys up in a Python dict with no borrow of its state held,
then hands the answers to the queue. One sequence counter numbers orders, retries and timeouts,
as the Python lane's does; held retries are kept by that sequence; submission ids come from one
process-wide counter.

## Policies

`lanes/policy.rs` reads a lane's former and selector at each lane operation, method by method,
with the builtin methods of [the core](core.md#builtin-policies):

| Policy method | Native |
|---|---|
| former `key`: the contract default (the unit's own `key`, None for the engine default or a raise), `Bucketed` around a builtin | yes; `Bucketed`'s `key_fn` and `inner` are read and called each time |
| former `due`: the default, `SizeOrWait` (with its own `_limit`) | yes; `wait` read each time |
| former `deadline`: the default, `Greedy`, `SizeOrWait`, `Bucketed` | yes; `size` read each time |
| former `take`: the default, `Greedy`, `SizeOrWait`, `Bucketed` | yes |
| selector `order`: the default, `LeastLoaded`, `Throughput`, `RoundRobin`, the `fallback` of `ByHint` and `Affinity` | yes; units' `capacity()` and `load()` are called as the Python policy calls them, and a sort key that is not a plain number is sorted by Python's `sorted` |
| selector `admits`: the default, `ByHint`, `Affinity` (with its own `preferred` and `item_key`) | yes; `Affinity` keeps its memory in the selector's `_pref`, as the Python selector does |
| any other method, or one an instance sets on itself | called back |

A former written in Python receives a `togeari._rs.Bucket`, a live view: `len`, `due`, `cap`,
`key`, `head()` and `items()` read the bucket when they are called. A selector receives the
lane. A policy that raises is a lane policy error, logged at the 1st, 2nd, 4th … one, with the
same fallback as the Python lane. Every call into Python (a policy, a bucket key's equality, an
engine, a hook) runs with no borrow of the lane's state held, so a policy may read the lane.

Under early forming, an item a unit no longer admits when it takes from its own queue moves, with
its order, to the first other unit in the pump's order that admits it (both cores; the Python
lane's `_respread`).

## Submissions

The lane hands each submit a `togeari._rs.Done(lane, sub)`; an engine calls it from any thread
(`done(ticket, results)`) and it posts `("done", lane, sub, results)` through the lane's
`on_done`. The dispatcher core reads the submission's items and unit from the native lane and
settles the results there. `pre_submit` plugins run through the lane's `hook_submit` after unit
choice, as in the Python lane, and `lane.submit` carries each item's (job key, address, order
key). A unit's `cancel` that raises an `Exception` is dropped, as in the Python lane.
