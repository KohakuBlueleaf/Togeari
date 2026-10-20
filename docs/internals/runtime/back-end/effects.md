# `ordered` and `mutates`

A node flagged `ordered` has an effect outside the job. A node with `mutates=[port, ...]` changes
an input object in place. Both must behave as in the sequential run even though younger
statements issue early. Code: `IssueStage.gate`, `issue_one` in
`src/togeari/core/runtime/backend/issue.py`; `WritebackStage.complete` in
`src/togeari/core/runtime/backend/writeback.py`; `RetireStage.retire` in
`src/togeari/core/runtime/retire/retire.py`; `Slot.muts`, `effective`, `Job.oldest_open_key`,
`Job.oldest_live_key` in `src/togeari/core/runtime/state.py`; `FrontEnd.op_call` in
`src/togeari/core/runtime/frontend/fetch.py`.

## Speculative

The inflight is **speculative** when either holds:

- `job.oldest_open_key() < inf.key`: an older inflight is waiting, ready, issued, or done with a
  fault not yet taken (it may still fail);
- `job.oldest_live_key() < inf.key`: an older live strand may still fetch older statements.

Both keys come from the job's heaps (`wheap`, `sheap`), whose stale tops are dropped on read, so
the check does not scan the window.

## Gate

`gate(inf)`, applied at issue to `ordered` and `mutates` calls, returns a `Gate` constant:

| Node | `knobs["mutates"]` | Speculative | Not speculative |
|---|---|---|---|
| `ordered` | any | `WAIT` | `GO` (then `mutates` rules, if any) |
| `mutates` | `copy-if-speculative` | `COPY` | `GO` |
| `mutates` | `wait` | `WAIT` | `GO` |
| `mutates` | `copy-always` | `COPY` | `COPY` |

`WAIT` puts the inflight into `blocked[job.id]`. Every `retire(job)` call moves the job's blocked
inflights back to `ready`, where the gate is evaluated again. An `ordered` node therefore runs
only when every older statement of its job completed without a fault and no older strand is
live.

An `ordered` node that is not `idempotent` carries `ext["ordered_once"]`: a `UnitError` fails it
with `UnitUnavailable` instead of retrying it on another unit ([unit health](../../lanes/health.md)).
`bind_region` drops any store mode on an `ordered` node.

## Versions of a mutated value

A `mutates` node changes an existing object that other names (aliases) may share. The slot
holding it gets versions keyed by order key; names are not renamed:

1. **Fetch** (`op_call`): for each `mutates` port, `version = Slot()` is appended to the input
   slot's (dereferenced) `muts` as `(call key, version)`; `inf.versions[port] = (before, version)`.
2. **Read**: `effective(slot, key)` follows the newest unsquashed version whose key is below the
   reader's key. A reader older than the `mutates` call sees the value before the change; a
   younger reader, through any alias, sees the version slot and waits for it.
3. **Writeback**: the version slot is filled with
   - the input object after the call (`values[port]`, which is the copy when the gate copied),
     when the node is inline, the submitting unit shares memory, or the port is not an output;
   - the output of the same name, when a unit with `shares_memory=False` returns it.
4. **Squash**: version slots of a squashed call become `SQUASHED`; `effective` skips them.

```
box = [1]
alias = box
(alias)total(t1)
(box, 5)bump()
(alias)total(t2)
```

`total(t1)` (older) reads the slot itself; `total(t2)` (younger) reads it through `alias`, sees
`bump`'s version, and issues after `bump` completes. If `total(t1)` has not finished when `bump`
is ready, `copy-if-speculative` gives `bump` a copy, so `t1` never sees the change.

A phi whose region contains a `mutates` call covers every bound name (`ALL` in the may-write
set), and `resolve_phi` moves the phi's `muts` to the slot it resolves to.

## Recording

For a recorded job, writeback keeps copies of the mutated port values in `inf.mutated`; retire
writes them as the fifth field of the call record, and replay applies them to the reference's
objects ([conformance](../../verification/conformance.md)).
