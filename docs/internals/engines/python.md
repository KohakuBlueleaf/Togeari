# Python call engine

`PythonCallEngine` (`src/togeari/engines/python/engine.py`) is a call engine that runs
`PythonCall` payloads (the `PythonAdapter`'s) on worker slots: threads in the machine's process,
or threads that each drive one spawned worker process. The contract it keeps is in
[writing an engine](../../guides/writing-an-engine.md).

## Arguments

| Argument | Default | Meaning |
|---|---|---|
| `workers` | 8 | slots |
| `mode` | `"thread"` | `"thread"` (`shares_memory=True`) or `"process"` (`shares_memory=False`) |
| `transport` | `"shm"` | process mode: `"shm"` (buffers of at least `shm_threshold` bytes cross in shared memory, the rest by pickle through the pipe), or `"pickle"` (everything through the pipe) |
| `shm_threshold` | `SHM_THRESHOLD` = 1 MiB | `engines/common/transport.py` |
| `start_timeout_s`, `stop_timeout_s` | 60, 5 | a worker process's ready wait; joining a slot |
| `imports` | `()` | modules every worker process imports once at start, before it reports ready |
| `name` | `"python"` | |

`spec()` is the identity: engine, name, mode, transport, workers, imports. `load()` reports
`running`, `waiting` (queued tickets) and in `extra` the live slot count, the state, and
counters (`items`, `ok`, `errors`, `cancelled`, `restarts`, `kills`, `shm_bytes`).

## Tickets and slots

`submit(work, done)` wraps the item in a `Ticket` (`src/togeari/engines/common/ticket.py`): one
submit, its items, its `done`, answered at most once (`answer`, `answer_all` return `False` when
already answered). While up, the ticket joins the ordered `pending` dict and one idle slot is
notified; otherwise it is answered with `UnitError(EngineStopped)` at once. `capacity()` is
`slots - running - pending` while up, else 0.

A slot loops on `engine.take(slot)` (waits on the engine's condition for a pending ticket), runs
the item, then `settle(slot, ticket, result, died)`: it counts the outcome and, unless the
process must restart, frees the slot (`running -= 1`) **before** answering the ticket, so a
`capacity()` read after the answer already counts the slot. `stays(slot)` then tells the slot
whether to go on. A slot whose process restarts stays busy until `release(slot)`.
`finish(item, raw)` decodes through the adapter: `Ok(outputs, True)` or `ItemError`.

| Slot | Runs |
|---|---|
| `ThreadSlot` | `item.payload().run()` on its thread |
| `ProcessSlot` | sends `("run", packet)` to its worker process (`engines/python/child.py`) and waits for `("ok", packet)` or `("err", packed exception)`; a reply with shared-memory blocks is acknowledged with `("release",)` |

The worker process (`child.main`) imports `imports`, sends `("ready", pid)`, then serves `run`,
`release` and `stop` messages; functions cross by reference, so they must be importable
top-level functions. While it runs, it is registered in the worker registry
(`engines/common/registry.py`: a `<pid>.json` file in `togeari-workers` under the system
temporary directory, removed when the worker returns), which `togeari doctor` reads to list
worker processes left over from an earlier run.

**Epochs.** Every `start()` begins a new epoch: the running and waiting counts start at 0, and
each slot carries the epoch it was opened for. A slot of an earlier epoch still finishing an
item (after a `stop()` and a new `start()`) changes no count of the current one, and
replacements opened for an old epoch exit at once.

## Failures

| Event | Effect |
|---|---|
| the function raises | `ItemError` |
| the worker process dies under an item | `ItemError(WorkerDied)`; the slot respawns its process (`respawn`); a slot that cannot respawn leaves the engine (`slot_lost`) |
| a worker process exits while its slot is idle | the slot leaves the engine (`idle_death`); a taken ticket is handed back to the front of `pending` |
| the last slot leaves | the engine goes `DOWN`; pending tickets get `UnitError`; new submits get `UnitError` |
| a slot left while others remain | a replacement starts in the background (`_revive`); `_revive` counts the missing slots again after each round, so slots lost while it runs are replaced in the same revive |

`ready()` is true while up; while down it starts the replacements in the background once and
returns false, so the lane's health polls bring the unit back.

`Monitor` (`engines/python/monitor.py`, process mode only) is one thread waiting on every worker
process's sentinel and a wake pipe. A process that exits while its slot is idle is reported
through `idle_death`; a process that exits under an item is the slot's own business and is
ignored until the slot has a new process (`poke` rebuilds the watched set).

## Cancel

`cancel(ticket)` answers the ticket `Cancelled()` if it was not answered yet, then:

- a waiting ticket leaves `pending`;
- a running ticket in process mode has its worker process killed (`interrupt`); the slot counts
  as busy until the respawned process is ready, then settles the kill as `Cancelled` (counted
  in `kills`);
- a running ticket in thread mode cannot be stopped; its slot stays busy until the function
  returns, and the late result is dropped (the ticket is already answered).

## Wakes

The engine inherits `add_wake` / `wake` from `EngineBase`
(`src/togeari/core/contracts/engine.py`), the optional contract for capacity that rises without
a completion. A lane registers one wake per unit ([lane](../lanes/lane.md)). The engine calls
`wake()`:

| When | Where |
|---|---|
| a restarted slot is free again | `release` |
| a ticket answered early (cancelled) frees its slot, without a restart | `settle` |
| background replacements came up | `_revive` |

An ordinary completion needs no wake: its answer reaches the dispatcher through `done`.

## Stop

`stop()` marks the engine stopped, answers every waiting and running ticket with
`UnitError(EngineStopped)`, closes the monitor, and joins the slots; worker processes are asked
to stop, then killed.
