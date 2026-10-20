# Admission, source and sink

How new jobs enter the pool. Code: `JobsStage.admit`, `take_source` in
`src/togeari/core/runtime/pool/jobs.py`; `Signals` in `src/togeari/core/runtime/pool/signals.py`;
`value_bytes` in `src/togeari/core/runtime/values.py`; the reader and writer threads in
`Machine.run` ([machine](../../construct/machine.md)).

## `admit()`

Runs in the cycle's admit phase. It returns at once without a source buffer, while stopping,
or while a sink record is held (`sink_pending`: back-pressure from a full sink queue). Then,
repeatedly:

1. With jobs resident, stop unless `knobs["admission"].allows(pool, signals)` and every
   `vote_admit` plugin agrees; `pool` is a `PoolView(jobs, bytes)`
   (`src/togeari/core/contracts/views.py`) and `signals` a fresh `Signals`. With no job
   resident, admission is not consulted. An `allows` that raises stops admission for this cycle
   (`policy_failed`: `machine_errors` and `policy_errors` counted, logged at the 1st, 2nd,
   4th ... error); once no job is resident the next item is admitted without it.
2. `take_source()`; stop when it gives nothing.
3. `start_job(item)` ([jobs](jobs.md)); an exception rejects the item as `MachineError`.
4. The new job fetches (`front.advance`) and its ready statements issue at once, so the next
   check sees the load the job just put on its lanes.

## Source wake protocol

`take_source()` reads `source_buf` without blocking. When it is empty:

1. `source_want = True` (the reader posts `("wake",)` after its next put and clears it);
2. one more read, which catches an item put between the two reads;
3. still empty and `run.source_finished`: the buffer is let go (`source_buf = None`), which ends
   the run once the pool drains.

The reader also posts `("wake",)` after its last item, so a finished source is noticed without
polling. The source buffer is bounded (`source_buffer`); a full buffer blocks the reader.

## Sink back-pressure

The sink queue is bounded (`sink_buffer`). Records that do not fit wait in `sink_pending` and
pause admission; the writer's `("wake",)` after its next take lets the dispatcher flush them
([jobs](jobs.md)).

## Signals

`Signals(dispatcher)` is a read-on-demand mapping, built once per `admit` call, so a rule pays
only for the keys it reads:

| Key | Value | Cost |
|---|---|---|
| `jobs` | resident jobs | live |
| `bytes` | `pool_bytes`: the estimated bytes resident jobs hold (after `release_freed`) | live |
| `window` | inflights in every window | first read, cached |
| `lanes` | `{name: lane.signals()}`: `queue`, `inflight`, `units_up`, `waiting` | first read, cached |
| `units` | `{name: {running, waiting, **extra}}` from `unit.load()` | first read, cached |

`to_dict()` is a plain copy; `Dispatcher.signals()` and the `signals` query return it.

`value_bytes(value)` estimates a value's size: 0 for `None`, bools and numbers; `len` of strings
and bytes; `nbytes` of buffers and arrays; for containers `sys.getsizeof` plus the items, the
first 64 sampled and scaled, two levels deep; `sys.getsizeof` otherwise.

A job's bytes (`job.bytes`) are what it holds now:

- its inputs, for the job's life;
- each output slot writeback filled with a non-empty value, while the slot lives;
- the results kept for a refetch after a fault ([faults](../retire/faults.md)), until they are
  used or dropped.

`hold(slot, job, size)` (`src/togeari/core/runtime/state.py`) adds the bytes and turns the slot
into a `HeldSlot`. A slot lives while a name map, a window entry or a waiting statement refers
to it; a retired statement lets go of its inputs, outputs, values and name view at retire.
When Python frees a `HeldSlot`, its `__del__` (on whatever thread frees it) appends
`(job, size)` to the dispatcher's `freed` deque. The dispatcher applies them in
`release_freed()`, which runs before each admission check, on a read of `signals["bytes"]`, at
each writeback that holds bytes, and at job end; a release for an ended job is skipped. The
job's bytes leave the pool at job end.

So a loop that rebinds one name holds about a window's worth of its values, not one per
iteration. Job stats report `bytes` (held at the end), `bytes_peak` and `bytes_produced` (every
value writeback held, summed).
