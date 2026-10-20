# Supervision

An error is contained to the smallest thing it belongs to. Code: `LoopStage.loop`, `died`,
`refuse_inbox`, `begin_stop` in `src/togeari/core/runtime/loop.py`; `JobsStage.job_error` in
`src/togeari/core/runtime/pool/jobs.py`; `DispatcherDied`, `JobHandle`, `Reply`, `RunState` in
`src/togeari/core/runtime/handles.py`.

| Error in | Effect |
|---|---|
| fetch, issue, delivery or retire of one job | `job_error(job, exc)`: the job fails with kind `MachineError`, every output missing; `stats["machine_errors"]` |
| starting a job | the item is rejected as `MachineError` |
| a `("call", fn)` event (unit starts, loads, lane wakes) | `event_error`: logged, `stats["machine_errors"]`; the loop goes on |
| `lane.results` for a `done` event | `event_error`; every item of the submission gets `ItemError(exc)` |
| a plugin hook, a posted plugin call | a plugin error ([hooks](../../plugins/hooks.md)): logged, change skipped, plugin disabled under `plugin_errors="disable"` |
| a query or command | the reply's error |
| a store `get` / `put` / `delete` | `store.error` event, `stats["store_errors"]`; a failed `get` is a miss |
| a snapshot | `snapshot.error` event; the job takes no more snapshots ([snapshots](../retire/snapshots.md)) |
| the source or the sink | `run.source_error` / `run.sink_error`, raised by `run` after the run ends |
| `issue.order` | `policy_failed`: `machine_errors` and `policy_errors` counted, logged at the 1st, 2nd, 4th ... error; the batch issues in arrival order |
| `admission.allows` | `policy_failed`; no job is admitted this cycle (with no job resident, admission is not consulted) |
| a lane's `select.order`, `select.admits`, `former.take` (in `pump`) | the lane's `stats["policy_errors"]`, logged at the 1st, 2nd, 4th ... error; the units up in their own order, every item admitted, the batch limit taken |
| `issue.priority` (called per statement inside `issue_one`) | `job_error` for that statement's job |
| anything else in the cycle | the dispatcher dies |

## Death

`loop()` runs `cycle()` and `after_cycle()` while `running`. Any exception that escapes calls
`died(exc)`:

1. `dead = exc`, `running = False`; a `machine.error` event with the error.
2. Every resident job is marked ended and answered with a failed `JobRecord` of kind
   `DispatcherDied`, to its handle or the sink queue.
3. `refuse_inbox()` answers every posted event: a submit with a `DispatcherDied` record, a query,
   command or stop with the `DispatcherDied` error, a `run` by setting its `done`.

The `finally` of `loop` lets the source buffer go and sets `run.done`. `Machine.run` then keeps
emptying the source buffer until the reader thread ends (nobody reads it after a death), and
raises `DispatcherDied`. `Machine._post` calls `refuse_inbox` itself after a death, and
`_ensure_thread` raises `DispatcherDied` for every later call. `died` runs once; a later call
only refuses what was posted since.

`abandon(exc)` is `died` called from another thread: `Machine.stop` uses it for a dispatcher
that did not stop in time, so every caller is answered before plugins, the bus and units stop;
whatever the loop is still running ends without effect on any job (every job is already
ended).

## Stop

`begin_stop(drain)` sets `stopping` and `run.stopped`. With `drain=False` every resident job is
cancelled (`cancel_job`, kind `Cancelled`, its handle answered). While stopping, `start_job`
rejects new items as `Cancelled` and `admit` takes nothing; the loop ends once no job is
resident.

## Handles

| Class | Holds |
|---|---|
| `JobHandle(key)` | `result(timeout)` waits for the `JobRecord`; `done()`; the first record set is kept, so a record set after `abandon` cannot replace the `DispatcherDied` one a caller read |
| `Reply` | `result(timeout=30.0)` returns the value or raises the error; `done()`; the first answer set is kept |
| `RunState` | `done` event; `source_finished`, `source_error`, `sink_error`, `stopped` |
| `SINK_END` | the sentinel that ends the sink writer |
