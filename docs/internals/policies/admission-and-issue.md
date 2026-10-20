# Admission and issue order

Two policies the dispatcher consults every cycle. Both live in the knob registry (`admission`,
`issue`), so `set_knob` swaps them between cycles; either may be given as the name of a policy a
plugin contributed. Contracts: `Admission`, `Issue` in `src/togeari/core/contracts/policies.py`;
builtins in `src/togeari/core/policies/` (re-exported by `togeari.policies`). Usage advice is in
[tuning](../../guides/tuning.md).

## Admission

`allows(pool, signals) -> bool`, called by `JobsStage.admit` before each new job while at least
one job is resident. Both arguments are read-only: `pool` is a `PoolView`
(`src/togeari/core/contracts/views.py`: `jobs` resident, their estimated `bytes`); `signals`
is a read-on-demand `Signals` mapping ([admission](../runtime/pool/admission.md)).

| Builtin (`admission.py`) | Allows while |
|---|---|
| `Fixed(n=64)` | `pool.jobs < n` |
| `TargetLoad(lane, waiting=8, max_jobs=4096)` | `pool.jobs < max_jobs` and the lane's `waiting + queue < waiting` (from `signals["lanes"]`); a lane name or a `Lane`; an unknown lane allows |
| `MemoryBound(max_bytes)` | `pool.bytes < max_bytes` |
| `All(*rules)` | every rule allows |

`TargetLoad` counts the lane's own queue (with held retries) as well as what the units report,
so a unit that reports no `waiting` still paces admission.

## Issue order

Issue policies see `ReadyView`s (`src/togeari/core/contracts/views.py`), read-only views of the
ready statements:

| `ReadyView` | Value |
|---|---|
| `job` | the job's id (rises with admission order) |
| `job_key`, `entry` | the job's key and entry name |
| `node`, `key` | the node name and the order key within the job |
| `hint` | the statement's `@hint` values (read-only mapping) |
| `lane`, `lane_fill` | the lane's name and its items waiting per unit up; `None` for inline nodes |

`order(views) -> list` returns the views in issue order; `priority(view) -> int` is passed to
`WorkItem.priority`, which orders the lane queue (higher first).

| Builtin (`issue.py`) | `order` | `priority` |
|---|---|---|
| `Fifo` | as they became ready | 0 |
| `OldestJobFirst` (default) | `(job, key)` | `-job` |
| `DeepestFirst` | key, descending | `len(key)` |
| `LaneAware` | inline first, then by `lane_fill` (read once per lane per call), then key | 0 |
| `HintPriority` | `hint["priority"]` descending, then key | the hint |

Statements whose views `order` leaves out are issued after the ones it returns, in their
arrival order, so a policy can reorder but never lose a statement.

## Hints

`Instr.hint` is the statement's `@hint` dict. The machine passes it as `WorkItem.ext["hint"]`.

| Key | Read by |
|---|---|
| `priority` | `HintPriority` |
| `unit` | `ByHint` |
| `max_wait_ms` | `SizeOrWait.due` |
| `timeout_s` | `IssueStage.execute`: `WorkItem.deadline = issue time + timeout_s` ([lane](../lanes/lane.md)) |
