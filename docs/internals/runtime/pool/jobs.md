# Jobs and records

`JobsStage` in `src/togeari/core/runtime/pool/jobs.py` starts jobs, ends them, and hands their
records to handles or the sink.

## Start

`start_job(item, handle=None)`:

1. While stopping: rejected as `Cancelled`.
2. `pre_admit` transform (`kind=SourceItem`, gate `entry`); `PluginBlockError` rejects it as
   `Blocked`.
3. A key already resident (`Dispatcher.keys`) is rejected as `DuplicateKey`.
4. Entry = `item.entry` or `machine.default_entry`; an unknown entry is rejected.
5. Inputs that are not parameters are rejected (`KirRuntimeError`); the top level has no
   parameters, so any input to it is rejected ("the top level: unknown inputs"). Each parameter binds to
   `const(input)`, or `const(fresh(default))`; a missing required input is rejected.
6. `Job(key, entry, inputs)` with the handle, `auto_key`, `stats["program"]` (the region's
   semantic hash).
7. Sampling: with `record` set, the job is recorded (`job.record = []`) with probability
   `record` (`record_rng`), and `sampled` follows that choice; with `record` off, `sampled` is
   drawn with probability `event_sample` (`event_rng`).
8. One strand at `entry.addr`, key prefix `()`, counter 0.
9. With a store: `job.inputs_dig` = the digest of the inputs as given, before any statement runs.
   Inputs that digest only by type (weak) leave it `None` and are counted (`note_weak`); such a
   job gets no positional store key and no snapshot.
10. With `snapshot_every` and a snapshot key: a snapshot stored for (region hash, job key,
    `inputs_dig`) is restored ([snapshots](../retire/snapshots.md)); automatic keys trigger a
    one-time warning (`warn_auto_key`).
11. `job.freed` = the dispatcher's `freed` deque; `job.bytes` = estimated bytes of the inputs,
    plus the restored values' slots after a resume except the entry's parameters, which the
    inputs already count (`hold`), added to `pool_bytes` (writeback
    holds each filled output slot's bytes while it lives,
    [admission](admission.md#signals)); the job joins `jobs`,
    `keys` and `dirty` and becomes `last_started`; `bus.sample(job.id, sampled)`; `job.admit`;
    `on_job_start({key, entry, inputs, recorded})`.

A rejection (`reject`) is a failed `JobRecord`, counted in `jobs_failed`, passed to
`on_job_end`, and sent through `emit_record`.

## End

`end_job(job, outputs, missing)`:

1. `ended`; every strand ended; every unfinished window entry squashed; `held` cleared; the
   kept results (`memo`) dropped; `release_freed()`.
2. `JobRecord(key, entry, ok, outputs, failure, missing, stats, record=job.record)`; stats add
   `seconds`, `steps`, `bytes` (held at the end), next to `bytes_peak` and `bytes_produced`.
3. Counters; the job leaves `jobs`, `keys` and `blocked`; its bytes leave `pool_bytes`.
4. A job that ended ok and took a snapshot in this run or resumed from one has its snapshot
   deleted from the store.
5. `job.end` (with `failure` and `stats`); `bus.forget_job`; `on_job_end(record)`;
   `emit_record(record, handle)`.

| Ending | Kind |
|---|---|
| `finish` with every output set | ok |
| a fault outside `@try` | the fault's kind |
| `finish` with missing `@def` outputs | `KirRuntimeError` |
| `job_error` | `MachineError` |
| `cancel_job` (command, `stop(drain=False)`) | `Cancelled` |

## Records

`emit_record(rec, handle)`:

- `pre_sink` transform (`kind=JobRecord`, gate `entry`). A `PluginBlockError` counts
  `records_blocked`; a waiting handle still gets the record, with `stats["sink_blocked"]` set to
  the message; nothing goes to the sink.
- A handle gets the record.
- Otherwise `to_sink(rec)`; with no run attached, `records_dropped` counts it.

`to_sink` puts the record on the bounded `sink_q` without blocking. A full queue holds it in
`sink_pending`; `flush_sink` sets `sink_wait` so the sink writer posts `("wake",)` after its next
take, and the cycle's drain phase flushes again. While a record is held, `admit` takes no new job
([admission](admission.md)).
