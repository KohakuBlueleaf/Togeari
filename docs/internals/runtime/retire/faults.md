# Faults

A fault marks a statement failed at once and squashes everything younger, but is **taken** only
when the statement reaches the retire position. Faults are therefore taken in program order and
no younger write is ever visible after one. Code: `FaultStage` (`fault`, `squash_younger`,
`squash`, `take_fault`) in `src/togeari/core/runtime/retire/faults.py`; `control_fault` in
`src/togeari/core/runtime/frontend/fetch.py`; `RuntimeFault` in
`src/togeari/core/runtime/kir.py`.

## Sources

| Source | Where | Catchable by `@try` |
|---|---|---|
| `ItemError` from an engine (node failure, `ItemTimeout`, `UnitUnavailable`, `Blocked`) | `complete` | yes |
| `Cancelled` for a live inflight | `deliver` | yes |
| inline node exception | `execute` | yes |
| adapter `decode` failure | `complete` | yes |
| `UndeclaredMutation` (`fingerprint_inputs`) | `complete` | yes |
| a result that is neither `Ok` nor `ItemError` (`TypeError`) | `complete` | yes |
| `RuntimeFault`: undefined name, missing input, bad condition, no switch case, non-iterable items, wrong output count, `UNDEF` input, `step_budget` exceeded | `FrontEnd.run`, `issue_one`, `complete` | no (kind `KirRuntimeError`) |

Errors of the machine itself while working on a job are not faults: `job_error` ends the job
with kind `MachineError` ([supervision](../loop/supervision.md)).

A `RuntimeFault` raised by a control instruction becomes a **control fault**: `control_fault`
consumes the instruction's key, creates an `Inflight` with `control = True` and a snapshot, ends
the strand, and faults it.

## Marking

`fault(inf, exc, catchable)`:

1. `unfinished -= 1` for a non-control inflight still waiting, ready or issued; `issued -= 1`
   for an issued one.
2. `inf.fault = {"error": {"kind", "node", "message"}, "catchable": ...}`; the kind is the
   exception's class name (`KirRuntimeError` for a `RuntimeFault`); `state = DONE`.
3. `squash_younger(job, inf.key, keep)`, with `keep` true only for a catchable fault inside a
   `@try` (`instr.try_id` set): the only faults whose handler refetches the squashed code.
   - every window entry with a larger key is cut (`Window.cut_after`); with `keep` and
     `squash_reuse` above 0 a completed call among them keeps its result (`keep_for_reuse`,
     below); each is squashed;
   - every live strand whose `next_key()` is larger ends (an `each` spawner included);
   - `final_names` / `final_key` are cleared when `final_key` is larger.

`squash(x)` drops the item from its lane queue or cancels its submission
([lane](../../lanes/lane.md)), marks `x` and its output and version slots `SQUASHED`, and counts
`squashed`. A result that later arrives for it counts as `wasted`.

Strands with a smaller `next_key()` keep running: a parked strand still walking its arm, or an
older strand, fetches only keys below the fault.

## Results kept for the refetch

`keep_for_reuse(x, cap, cap_bytes)` keeps the result of a squashed statement that:

- is `DONE` without a fault;
- is a call whose node has neither `ordered` nor `mutates`;
- is bound without `store="positional"` (a positional result belongs to its address and order
  key, and keys after a handler depend on the fault);
- has a result of at most `cap_bytes` bytes (`value_bytes`);
- has inputs that digest by content (`digest_with_weak`, no weak type).

The entry `(inputs digest, result, bytes)` goes into the job's `Memo` (`job.memo`: entries by
address in insertion order, their count `n` and `bytes`). Its bytes join the job and the pool.
Past `cap` entries (`knobs["squash_reuse"]`) or `cap_bytes` bytes (`knobs["squash_reuse_bytes"]`,
64 MiB; None for no byte cap) per job, the oldest address's oldest entry is dropped.

At issue, a statement whose address has entries digests its inputs. `take_reused(inf, dig)`
looks for an entry with the same digest. A match leaves the memo, so each result serves one
refetch. Its bytes leave the job and the pool, and `reused` is set. `reused` is counted in the
job's and the machine's stats. The statement then completes through `complete` with
`Ok(result, True)`, as a store hit does. `stmt.done` names `unit="reused"`.

A reused statement is recorded like any call: `(node, inputs digest, "ok", kept copy, None)`.
Conformance in replay and re-execute modes sees each call once, as the reference makes it.
`end_job` drops the memo. `squash_reuse=0` keeps nothing, and the refetched calls are issued
again.

## Taking

`retire` reaches the faulted inflight once it is the window head and no live strand is older.
`take_fault(job, x)`:

1. `pop_first()` from the window (`RETIRED`); for a recorded job and a non-control inflight,
   append `(node, dig, "error", {kind, node, message}, None)` to `job.record`.
2. `on_fault({"job": id, "key": job key, "kind", "node", "message"})` (gate `entry`);
   `fault.taken` with `addr`, the job's squashed count and the error fields.
3. Restore `(names, frames, prefix, counter)` from `x.snapshot`.

**Inside a `@try`** (`catchable` and `instr.try_id` set), with `info = code.tries[try_id]`:

- keep the frames not opened inside `info.body`; a kept frame with a join is replaced
  (`Frame.replace`) without it (its continuation strand was younger and is squashed), an
  `"iter"` frame also with `reconv` set to the `each`'s reconvergence point;
- bind `info.error_name` to `const({"kind", "node", "message"})` in `NameMap.of(names)`;
- add a handler strand at `info.handler` with the restored names and frames, at the faulting
  statement's `(prefix, counter)`.

The handler and everything after it is fetched again with keys continuing from the faulting
statement's: code after the handler gets keys that depend on the handler's length, not
necessarily the keys its squashed statements had.

**Outside any `@try`**, or for an uncatchable fault: the job fails with
`failure = {kind, node, message}`. Its outputs are the entry's outputs read from the restored
names at the fault's key; unfilled ones are listed as missing. `end_job` sends the record.

## Example

```
(a)fetch(x)
@try:
    (x)parse(y)
@except err:
    y = "?"
(y)store()
```

`fetch` at `(0,)`, `parse` at `(1,)`, `store` at `(2,)`. If `parse` returns `ItemError`, `store`
(waiting on `y`) is squashed at once. When `parse` reaches the window head, the handler strand
starts with `err = {"kind": ..., "node": "parse", "message": ...}`, fetches `y = "?"` at `(1,)`
and `store` again at `(2,)`.
