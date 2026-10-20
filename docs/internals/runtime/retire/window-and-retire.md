# Window and retire

Each job's `window` holds its unretired inflights in order-key order. Retire pops its head,
which is the job's sequential order. Code: `FrontEnd.blocked_by_caps` in
`src/togeari/core/runtime/frontend/fetch.py`; `RetireStage` in
`src/togeari/core/runtime/retire/retire.py`; `Window` in `src/togeari/core/runtime/state.py`.

## Caps and progress

`blocked_by_caps(st)` lets a strand fetch while both hold:

- `job.unfinished < knobs["window"]` (inflights not yet done);
- `len(job.window) - job.unfinished < knobs["retire_buffer"]` (done, waiting to retire).

At either cap:

- a strand whose `next_key()` is not `job.oldest_live_key()` waits;
- the oldest live strand fetches, so the oldest unfinished statement can always be fetched and
  issued;
- the oldest live strand that is an `each` spawner waits while `job.oldest_open_key()` is older
  than it: that older work will free the window ([each](../front-end/each.md)).

Control instructions, `assign` and `jump` make no inflight. A control fault's inflight sits in
the window but not in `unfinished`.

## Retire

`retire(job)` loops over the window head `x`, with `bound = job.oldest_live_key()`:

1. Stop if `x` is not `DONE`.
2. Stop if `x.key >= bound`: a live strand may still fetch an older statement.
3. A faulted `x` is taken ([faults](faults.md)); `bound` is read again; continue.
4. Otherwise `pop_first()`, `RETIRED`, `retire_one(job, x)`.

`retire_one(job, x)`:

- with `assert_alias`: every input `x` read, every output and every version it wrote must be
  `READY`, else the job's failure becomes `AliasCheck`;
- a recorded job and a `call`: append `(node, dig, "ok", kept, mutated)` to `job.record`; past
  `knobs["record_limit"]` calls the record is dropped and `stats["record_skipped"]` says why;
- a `positional` store `put` of the copy taken at settle (`inf.kept`), unless the store served
  it or the result could not be copied (`stats["store_uncopied"]`);
- `stmt.retire`.

Then, unless the job ended:

- every `snapshot_every` retired statements (and with a store, and snapshots not stopped),
  `take_snapshot` ([snapshots](snapshots.md));
- `blocked[job.id]` moves back to `ready` ([effects](../back-end/effects.md));
- when something retired and the job has runnable strands, the job is dirty again (strands
  waiting at a cap may fetch);
- no live strand and an empty window: `finish(job)`.

A value is visible to younger readers as soon as writeback fills its slot; retire does not gate
reads. It orders recording, positional store writes, fault taking and job end.

## Job end

`finish(job)` collects the outputs from `job.final_names` (set when a strand fetched `end`; none
when the end was never reached):

| Entry | Output names |
|---|---|
| `@def` | the entry's outputs |
| top level | every final name without `__` (inline renames) |

Each name is read through `effective(slot, (inf,))`. An unset, unfilled or `UNDEF` output is
missing; a `@def` entry with missing outputs fails with `KirRuntimeError`
`"@def main: outputs [...] not set"`.

With `assert_alias`, `alias_at_end(job, final)` fails the job with `AliasCheck` when a final
name is not filled, or when a mutable value (list, dict, set, bytearray, object with `__dict__`)
of its final names is also held by another resident job's live names or window input values.

`end_job` then builds the record ([jobs](../pool/jobs.md)).
