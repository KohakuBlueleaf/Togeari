# Issue and writeback

From a fetched `Inflight` to filled output slots. Code: `IssueStage` in
`src/togeari/core/runtime/backend/issue.py`; `WritebackStage` in
`src/togeari/core/runtime/backend/writeback.py`.

## Ready

`admit_inflight(inf)` adds it to the window, counts `unfinished` and `calls`, emits
`stmt.fetch`, and calls `check(inf)`.

`check(inf)` looks at each input through `effective(slot, inf.key)`:

- `PENDING`: wait on that slot and stop (a fill re-runs `check`);
- `SQUASHED`: stop; the inflight is younger than a fault and is squashed with it;
- all filled: `state = READY`, append to `ready`, `stmt.ready`.

## Issue

`issue()` takes the whole `ready` list, wraps each inflight in a `ReadyView`
(`src/togeari/core/contracts/views.py`: `job`, `job_key`, `entry`, `node`, `key`, `hint`,
`lane`, `lane_fill`) and issues in the order `issue_order(batch)` gives: the views
`knobs["issue"].order(views)` returns, keeping only views of this batch and each statement
once, then the statements it left out in arrival order. An `order` that raises gives arrival
order (`policy_failed`: `machine_errors`, `policy_errors`, a log line at the 1st, 2nd, 4th ...
error). Each inflight still `READY` goes to `issue_one`, unless its job is paused (then it waits
in `job.held` until `resume_job`). An exception in `issue_one` is `job_error`
([supervision](../loop/supervision.md)).

`issue_one(inf)`:

1. `ordered` or `mutates`: `gate(inf)` gives `GO`, `COPY` or `WAIT`; `WAIT` parks it in
   `blocked[job.id]` until the job's next retire ([effects](effects.md)).
2. `values = {port: effective(slot, key).value}`. An `UNDEF` input is an uncatchable
   `RuntimeFault`.
3. `COPY`: each `mutates` port's value becomes `copy_value(v)` (`v.__togeari_copy__()` or
   `copy.deepcopy`); `inf.copy = True`; `stats["mutates_copies"]`.
4. With the job recorded or `fingerprint_inputs` on, or a result kept for this address
   (`job.memo`): `dig = digest_with_weak(values)`. For the record and the fingerprint check,
   weak types are counted per job and machine (`note_weak`, `stats["weak_digests"]`).
5. A kept result with the same inputs digest completes the statement (`take_reused`,
   [faults](../retire/faults.md#results-kept-for-the-refetch)).
6. Otherwise `execute(inf)`.

## Execute

| Binding | `execute` does |
|---|---|
| `Inline` | calls `fn(**values)` now; `Ok(result, finished=True)` or `ItemError(exc)`; `note_inline`; `job.issue`; `complete` |
| store mode set and a store given | `store_key(inf)`; a hit (`store_get` not `MISS`) sets `hit`, counts `store_hits`, completes at once with `Ok(hit, True)`; a miss counts `store_misses` |
| lane | `WorkItem(node, values, adapter, protocol=defaults.protocol, priority=issue_priority(inf), owner=inf)` (the policy's `priority(ReadyView(inf))`, 0 when it raises); `deadline = now + @hint timeout_s` when the hint is set; `ext["job_key"]`, `ext["hint"]`; `ext["ordered_once"]` for an `ordered` node not `idempotent`; `job.issue`; `lane.enqueue(item, now)`; `stmt.issue`, `lane.enqueue` |

`note_inline` keeps an `InlineStat` per node (last 1000 times, count, total). At the 32nd call
and every 1024th it logs one warning when the median exceeds `Inline.warn_us`, else
`knobs["inline_warn_us"]`.

### Stores

| Mode | Key | Read | Written |
|---|---|---|---|
| `content` | `digest((region semantic hash, node, inputs digest, machine.identity(adapter, lane)))` | at issue | at writeback, unless served by the store |
| `positional` | `digest((region semantic hash, job key, job.inputs_dig, address − region start, order key))` | at issue | at retire, the copy taken at settle, unless served by the store |

No key is made, so the statement runs, when the statement's inputs (`content`) or the job's
inputs (`positional`: `job.inputs_dig` is `None`) digest only by type: `stats["store_weak"]`. `machine.identity` is the adapter's identity and the lane's
`engine_identity()` ([lane](../../lanes/lane.md)). `store_get`, `store_put`, `store_delete`
catch store errors (`store.error`, `stats["store_errors"]`); a failed get is a miss. A store
without `delete` gets `put(key, None)`. Builtin stores keep their own copy on `put` and return a
fresh copy on `get` (`MemoryStore` in `src/togeari/stores/memory.py`).

## Lane hooks

| Method | Called by | Does |
|---|---|---|
| `lane_done(lane, sub, results)` | engine threads | posts `("done", lane, sub, results)` |
| `before_submit(lane, unit, items)` | `Lane._submit` | the `pre_submit` transform per item (`kind=WorkItem`, gates `lane` and `entry`, context `unit=`, `lane=`); a block delivers `ItemError(Blocked)`; a replacement item keeps the original's `owner` and `ext` and becomes `inf.item` |

## Writeback

`deliver(item, result, unit=None)` emits `lane.result` (queue and service times) and:

- `Cancelled` for an inflight that is not squashed becomes `ItemError(RuntimeError("cancelled"))`;
- anything else goes to `complete(item.owner, result, unit)`;
- an exception is `job_error`.

`complete(inf, result, unit)`:

1. A squashed inflight or an ended job: count `stats["wasted"]`, drop.
2. Only issued or ready inflights continue.
3. `post_result` transform (`kind=(Ok, ItemError, UnitError, Cancelled)`, gates `entry` and
   `lane`, context `node=`, `job=`); the job becomes dirty.
4. `ItemError` or `UnitError`: `fault(inf, error, catchable=True)`
   ([faults](../retire/faults.md)); `Cancelled`: a catchable `RuntimeError("cancelled")` fault;
   any other non-`Ok` value is a catchable `TypeError` fault.
5. `Ok(value, finished=False)` with a work item: `value = item.finish(value)`; an exception is a
   catchable fault.
6. `fingerprint_inputs`, node without `mutates`: the inputs' digest must equal `dig`, else an
   `UndeclaredMutation` fault.
7. Outputs (`bind_outputs`): with one signature output or one written output the value binds whole; otherwise
   `list(value)` must have one element per written output (else an uncatchable fault). Each
   named output slot is filled, waking its waiters, and holds its value's estimated bytes on
   the job and the pool while it lives (`hold`, [admission](../pool/admission.md#signals)).
8. `mutates` versions are filled ([effects](effects.md)); `shares_memory` is read from the
   submitting `unit` (inline nodes share).
9. `settle`: for a recorded job, and for a result a `positional` store writes at retire,
   `inf.kept` is a copy taken now (`kept_copy`); a recorded job also keeps `inf.mutated`. A
   later in-place change by a younger `mutates` call reaches neither the record nor the store.
   A value that cannot be copied counts `record_uncopied`; such a positional result is not
   stored (`stats["store_uncopied"]`).
10. The work item drops its inputs and built payload (`WorkItem.release`).
11. `issued -= 1`, `DONE`, `t_done`, `unfinished -= 1`; a `content` store `put`; `stmt.done`
    with `unit` = the unit name, `"store"`, `"reused"` or `"inline"`.

At retire the statement also drops its work item (`x.item = None`) with its values, so a retired
statement in the window holds none of its inputs or payload.
