# Snapshots

With `snapshot_every=N` and a store, a job writes its state to the store at retire boundaries,
and a later run of the same job resumes from it. Code:
`src/togeari/core/runtime/retire/snapshot.py`, called from `RetireStage.take_snapshot`,
`JobsStage.start_job` and `JobsStage.end_job`.

## Key and lifetime

`snapshot_key(region hash, job key, inputs digest) = digest(("snapshot", hash, key, inputs
digest))`, where the inputs digest is `job.inputs_dig` (`RetireStage.snapshot_key`; `None`, so no
snapshot is taken or read, when the job's inputs digest only by type). A job resumes only from a
snapshot of the same program, key and inputs.

| When | Store operation |
|---|---|
| a snapshot is taken | `put(key, blob)`; `job.snap_at` = retired count; `stats["snapshots"] += 1` |
| a job starts with `snapshot_every` set | `get(key)`; a blob is restored |
| a job that took a snapshot or resumed from one ends ok | `delete(key)` (or `put(key, None)` without `delete`) |
| a job fails | the snapshot stays: it is the resume point of the next run |

Jobs whose keys are source positions (`auto_key`) log one warning, since positions do not
identify a job across runs.

## Capture

After a `retire(job)` pass, when `stats["retired"] - job.snap_at >= N` and snapshots are not
stopped, `capture(job)` returns bytes, or `None` when the job is not at a boundary:

1. The window must be empty and exactly one strand live, not an `each` spawner.
2. A strand parked at a branch is captured as just before that branch: its top join frame is
   dropped, `pc = frame.branch`, and the counter is the saved one minus one.
3. Every name must read a filled value through `effective(slot, (inf,))`.

Remaining frames may hold an open join: with the window empty and one strand live, the join's
continuation has ended without leaving work (retire stops at the oldest live strand), and the
restored strand, whose frames come back without joins, runs past the reconvergence point
itself. A loop whose exit condition parked once is captured this way mid-loop.

The state is `pickle.dumps` (protocol 5) of:

| Key | Content |
|---|---|
| `version` | the layout (`VERSION`, 1) |
| `pc`, `prefix`, `counter` | where the strand continues |
| `frames` | per frame: `reconv`, `saved`, `branch`, `kind`, `each`, `items`, `i`, `base`, and `joined` (it had a join) |
| `values` | `{name: value}` |
| `stats`, `steps` | the job's stats and executed-instruction count |
| `record` | the job's conformance record (or `None`) |

An exception from `capture` (a value that does not pickle) sets `job.snap_off`, writes
`stats["snapshot_error"]`, counts `snapshot_errors`, logs, and emits `snapshot.error`; the job
continues and takes no more snapshots.

## Restore

`restore(job, blob, code)`:

- a state of another `version` (or none) is no snapshot: a warning, and the job starts from its
  entry;
- frames are rebuilt (`Frame(**d)`) without joins; an iteration frame that had one (the last
  `each` iteration running alone after the strand past the loop ended) gets the `each`'s
  reconvergence point from `code.tables(each).reconv` as its `reconv`, as a taken fault's
  handler does, so the restored strand runs its `ITER_END` and leaves the loop itself;
- the undefined value (`UNDEF`) pickles as itself, so a name no path wrote stays undefined;
- every value becomes `const(value)` in a fresh `NameMap`; unpickled values belong to this job
  alone, so they are not copied;
- the job's strands are ended and one strand is added at the stored `pc`, `prefix`, `counter`;
- stats are merged, `stats["resumed"] = True`, `steps` and `snap_at` restored;
- a job sampled for recording takes the stored record, so its record covers the whole job; when
  the snapshot has none, `stats["record_skipped"]` says so.

An unreadable blob logs a warning and the job starts from its entry.

`load(store, key, on_error=None)` reads the blob: `MISS` or `None` is no snapshot; a store
`get` that raises is passed to `on_error` (else logged) and also reads as no snapshot.

Trust: `restore` unpickles what the store returns, and unpickling runs code the data names, so a
store that holds snapshots must be writable only by trusted writers.

Order keys continue from the stored `(prefix, counter)`, so positional store keys of the resumed
statements equal those of the first run.
