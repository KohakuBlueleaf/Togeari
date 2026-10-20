# Conformance

A recorded job is run again by `kohakunode`'s reference interpreter, with a call hook that
checks every node call against the machine's record; the outputs, or the failure, must be equal.
Code: `src/togeari/conformance/replay.py`, `src/togeari/core/contracts/digest.py`,
`src/togeari/plugins/record/`, `src/togeari/cli/replay.py`, `src/togeari/cli/commands/conform.py`.

## Recording

- `record=<rate>` samples jobs at `start_job` (`Dispatcher.record_rng`, seeded by `seed`); a
  sampled job has `job.record = []` ([jobs](../runtime/pool/jobs.md)).
- At issue, a sampled job's inflight stores `dig = digest(values).hex()`.
- At writeback, the result (after `post_result`) and every changed `mutates` input are copied
  (`kept_copy`), so a later in-place change cannot alter the record.
- At retire, in order-key order (the sequential order), each call appends one tuple:

| Field | `ok` record | `error` record |
|---|---|---|
| node | `instr.node` | `instr.node` |
| digest | hex digest of the input values | same (`None` when the fault came before issue) |
| kind | `"ok"` | `"error"` |
| value | the copy of the result taken at writeback | `{kind, node, message}` |
| mutated | `{port: copy of the value after the change}` for `mutates` calls, else `None` | `None` |

Inline calls are recorded like lane calls. Control faults (no node call) are not recorded. Past
`record_limit` calls the record is dropped (`stats["record_skipped"]`). A resumed job continues
the record stored in its snapshot. `end_job` puts the list into `JobRecord.record`.

## Digests

`digest(value)` is a sha256 over a canonical feed (`_feed`); `digest_with_weak(value)` also
returns the type names compared by type only. Every leaf goes in as a self-delimiting chunk,
`chunk(h, tag, data)`: a one-byte tag, the data's length (8 bytes), the data. So no two
different values feed the same bytes.

| Value | Fed as (tag) |
|---|---|
Rows are tried in order:

| Value | Fed as (tag) |
|---|---|
| `None`, `bool`, `int`, `float`, `complex` | type name and repr (`v`) |
| `str` (`s`), bytes-like (`b`) | the raw bytes |
| has `__togeari_digest__` | its returned bytes (`d`) |
| a type registered with `register_digest(cls, fn)` (or a subclass, a list or dict subclass included) | the bytes `fn(value)` returns (`r`); an engine package registers its own array type this way |
| `list`, `tuple`, `dict`, `set`, `frozenset` | type, length, items recursively (dict keys and set items sorted by `_order`: strings by value, others by their digest); a container met again inside itself is weak (`w`) |
| array-like (`shape`, `dtype`, `tobytes`) | shape and dtype (`a`), bytes (`A`) |
| anything else | pickle (`p`), with every set or frozenset inside pickled with its members in `_order` (`_OrderedSetPickler`), so equal objects pickle alike in every process; on failure the type name (`w`), reported as weak |

The type cache of registered functions is filled under the registration lock, so a fill never
caches a lookup that a concurrent `register_digest` has made stale.

`register_digest` remembers, per registered type, the modules behind it (the caller and the
function's module); `unregister_digest(cls)` drops a type with its modules; `digest_providers()`
lists the modules of the current registrations, and each recorded run stores the list so a replay can
import them first. Weak digests are counted per job (`stats["weak_digests"]`) and per machine.

## Replay

`replay(job_program, record, inputs, mode="replay", pure=None, engine_errors=True,
step_budget=STEP_BUDGET)` runs `Interpreter(job_program.written, job_program.env,
hook=_Hook(...), step_budget=step_budget)` (`STEP_BUDGET` = 1 000 000), then
`interp.call(entry, inputs)` (or `run()` and `source_values()` for the top level), and returns
`(status, value, notes)`:

| Status | Value |
|---|---|
| `"ok"` | the reference outputs |
| `"error"` | `{kind, node}` from a `NodeFailure`, or `{kind: "KirRuntimeError", node: None, message}` |
| `"mismatch"` | the reason; also when the reference made fewer calls than recorded |

The hook, per node call of the reference:

1. pops the next record; none left is a `Mismatch` (a `KirRuntimeError` subclass, never caught
   by `@try`);
2. checks the node name and `digest(inputs).hex()` against the record;
3. under `reexecute` and a pure node: `rerun` (below);
4. an `error` record raises an exception whose class name is the recorded kind;
5. applies each recorded mutated value, copied, to the reference's input object in place
   (`assign_in_place`: list slice, dict / set update, `__dict__` copy, or equality);
6. returns a copy of the recorded value (`_fresh`), so the reference never changes the record.

### Modes

| Mode | Pure nodes | Other nodes |
|---|---|---|
| `replay` | replayed | replayed |
| `reexecute` | run again and compared | replayed |

`pure` names the nodes run again: `None` (none), a collection of names, or a predicate over the
node name (`pure_test`; a bare string is refused). `rerun` calls the node function on the
reference's inputs:

| Node | Record | Result |
|---|---|---|
| returns | `ok` | the digests of its result and of each mutated input must equal the record's; returns its own result |
| returns | `error` | `engine_errors=True`: the recorded error is raised and a note added (an injected or engine-side failure); `False`: mismatch |
| raises kind K | `error` with kind K | the node's exception is raised |
| raises | `error` with another kind | `engine_errors=True`: the recorded error, with a note; `False`: mismatch |
| raises | `ok` | mismatch |

Re-execution catches a result delivered to the wrong statement or job, which `replay` alone
cannot see.

## Verdict

`conform(job_program, job_record, inputs, mode="replay", pure=None, engine_errors=True,
step_budget=STEP_BUDGET)` replays
`job_record.record` and returns `Verdict(ok, reason, reference, machine, details)`; `details`
holds the hook's notes.

| Reference | Machine | Verdict |
|---|---|---|
| not recorded | — | not ok |
| mismatch | — | not ok, the reason |
| error | ok | not ok |
| error | failed | ok when the kinds are equal and, unless the kind is `KirRuntimeError`, the nodes are equal |
| ok | failed | not ok |
| ok | ok | ok when the outputs are equal |

Each job replays against the job program of its own entry, so jobs of several programs in one
machine are checked independently.

## Record directories

`Recorder(path, libraries=None, queue_size=100_000, close_s=10.0)` (`plugins/record/recorder.py`)
keeps each job's admitted inputs while `record` is set (`on_job_start`) and, at `on_job_end` of a
job with a record, hands it to an `ArchiveWriter` (its own thread; tasks past `queue_size` are
dropped and counted). At unload, `ArchiveWriter.close(timeout=close_s)` drains for at most about
twice `close_s`; then, under the writer's lock, the record files close, and tasks still queued
write nothing. `RunReport` passes its own `close_s` to its embedded recorder. Each run writes its
own folder:

| Path | Content |
|---|---|
| `programs/<semantic hash>.json` | one program, written before its first job: source, text, entry, passes, hash, library refs (`module:attr`), signature paths; one file per program, so the writers of several runs never overwrite each other |
| `runs/<run>/run.json` | the run id (start time, process id, random suffix), start time and `digest_providers` |
| `runs/<run>/record.kv` | job key → pickled job entry (with KohakuVault installed) |
| `runs/<run>/record.pkl` | the pickled job entries in sequence (otherwise) |

A job entry holds `run`, `key`, `entry`, `program` (the semantic hash it ran, from
`stats["program"]`), `inputs`, `ok`, `outputs`, `failure`, `missing`, `stats`, `record` and
`weak` (paths of values stored by `repr` because they do not pickle). The `recorder` query gives
the path, run, written and dropped counts. `RunReport` writes the same files through an embedded
`Recorder`.

`Archive(path, runs=None)` reads a record folder back: `programs` (by hash), `runs()`,
`run_info(run)` and `jobs()`.

`togeari conform DIR [--lib module:attr] [--run ID] [--reexecute NODE] [--step-budget N]`
(`replay_dir`) reads
every run (or the named ones). Before a run's first job it imports the run's digest providers
(`import_providers`; a failure stops with a message naming them). It rebuilds each program by
hash with `build_job_programs` (the semantic hash must match; libraries that cannot be imported
stop it with a message), and replays every job, in `reexecute` mode for the named nodes. It
prints each failure (reference and machine outcome, the job's weak values and weak digests),
then one line per run and the total:

```
run 20261020-182106-108636-d20bd7: 3/3 conform; 0 with weak values; 0 weak digests
3/3 jobs conform; 0 with weak values; 0 weak digests
```

The exit code is 0 only when every job conforms.
