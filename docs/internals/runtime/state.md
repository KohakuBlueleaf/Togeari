# Job state

`src/togeari/core/runtime/state.py` defines everything a resident job is made of. Only the
dispatcher thread reads or writes these objects. States are namespace classes: `SlotState`
(`PENDING` 0, `READY` 1, `SQUASHED` 2), `StrandState` (`RUNNABLE`, `PARKED`, `STALLED`, `ENDED`)
and `InfState` (`WAITING`, `READY`, `ISSUED`, `DONE`, `RETIRED`, `SQUASHED`).

## Slot

One renamed value.

| Field | Meaning |
|---|---|
| `value`, `state` | the value once filled; a `SlotState` |
| `waiters` | strands and inflights to wake when it fills |
| `forward` | for a resolved phi: the slot it stands for |
| `phi` | true for a phi slot |
| `muts` | `[(order key, version slot)]` of `mutates` calls on this value; when such a call retires, entries older than it and squashed ones are dropped (`fold_versions`), so the list holds at most the unretired calls plus the newest retired one |
| `id` | a process-wide counter, for display and the `job` query |
| `size`, `owner` | set on a held slot only: its value's estimated bytes and its `Job` |

A `HeldSlot` is a `Slot` whose bytes count toward its job while it lives. When Python frees one,
on any thread, its `__del__` appends `(owner, size)` to `owner.freed`, the dispatcher's deque
([admission](pool/admission.md#signals)).

| Function | Gives |
|---|---|
| `hold(slot, job, size)` | turns a filled slot into a `HeldSlot` holding `size` bytes on `job`; the bytes added (0 for an empty value or a slot already held) |
| `const(value)` | a `READY` slot |
| `deref(s)` | follows `forward` to the slot a phi resolved to |
| `effective(s, key)` | the version a reader at `key` sees: `deref`, then repeatedly the newest unsquashed `muts` version whose key is below `key` |

`effective` is the only way readers look at a slot; see [effects](back-end/effects.md).
`kir.undefined()` (`src/togeari/core/runtime/kir.py`) makes a fresh `READY` slot holding the
`UNDEF` sentinel for each phi that resolves to "never written", so no slot is shared between
jobs.

## Name maps

| Class | Shape |
|---|---|
| `Names` | an immutable view: a shared `base` dict overlaid by a `delta` dict; `get`, `to_dict`, `keys`, `items`, `in` |
| `NameMap` | a writable map: `base` plus a private `delta`; `set` copies a shared delta first; a delta over `FOLD = 32` entries is folded into a new base |

`NameMap.snapshot()` marks the delta shared and returns `Names(base, delta)`, so a snapshot
before every call (the fault restore point) costs nothing, and the next write copies only the
delta. `NameMap.of(view, extra)` starts a map from a view with `extra` names written over it.

## Frame and Join

A `Frame` is a region a strand is inside. Frames are never changed once pushed: a strand's
`frames` is a tuple, shared by snapshots and new strands, and `Frame.replace(**changes)` makes a
changed copy.

| Field | Meaning |
|---|---|
| `reconv` | address where the region ends; reaching it pops the frame |
| `saved` | `(prefix, counter)` restored on pop |
| `join` | the `Join` this strand resolves at `reconv` (a parked strand, an eager iteration) |
| `branch` | address of the control instruction that opened it |
| `kind` | `"branch"` or `"iter"` |
| `each`, `items`, `i`, `base` | for `"iter"`: the `each` address, the item list, the index, the `each`'s own key |

A `Join` holds `phis: {name: phi slot}`, the may-write `table` used for early resolution,
`resolved`, and `all_names` (the region may write any name).

## Strand

| Field | Meaning |
|---|---|
| `pc` | address in code memory |
| `prefix`, `counter` | the next order key is `(*prefix, counter)` (`next_key()`) |
| `frames` | tuple of open `Frame`s, innermost last |
| `names` | its `NameMap` (`None` for a spawner) |
| `state` | a `StrandState` |
| `resume` | `"parked"` or `"stalled"` while waiting on a control operand; `False` otherwise |
| `queued` | in the job's runnable list |
| `spawn` | the `EachSpawn` of an eager `each` this strand starts iterations for ([each](front-end/each.md)); `None` for an ordinary strand |

## Inflight

A fetched, unretired statement.

| Field | Meaning |
|---|---|
| `job`, `instr`, `key` | where it comes from; `key` is its order key |
| `inputs` | `[(port, Slot)]` |
| `outs` | output slots (`None` for `_`) |
| `versions` | `{port: (slot before, version slot)}` for `mutates` ports, else `None` |
| `state` | an `InfState` |
| `fault` | `{"error": {kind, node, message}, "catchable": bool}` once faulted |
| `snapshot` | `(names view, frames, prefix, counter)` just before the statement |
| `values` | input values at issue |
| `item` | the `WorkItem` when sent to a lane |
| `copy` | the `mutates` ports got copies |
| `result` | the node result after writeback |
| `kept`, `mutated` | recorded jobs: a copy of the result and of changed `mutates` inputs, taken at completion |
| `t_issue`, `t_done` | perf-counter times |
| `store_key`, `hit` | the store key; served by the store |
| `control` | a control instruction's runtime fault (no node call) |
| `dig` | hex digest of `values` (recording, `fingerprint_inputs`) |
| `reused` | completed with a result kept from a squashed run of the same statement ([faults](retire/faults.md#results-kept-for-the-refetch)) |

At retire an inflight lets go of `values`, `result`, `snapshot`, `kept`, `mutated`, `versions`,
`inputs` and `outs`, so the slots and values it referred to can be freed.

## Window

`Window` keeps a job's inflights sorted by key in a list with a moving `head`: `add` appends when
the key is the largest (else `insort` after the head), `first` / `pop_first` read and advance the
head (the list is trimmed once the head passes 256 and half the list), and `cut_after(key)`
removes and returns every inflight with a larger key (binary search).

## Job

| Field | Meaning |
|---|---|
| `id`, `key`, `entry`, `inputs`, `handle` | identity, the `Entry`, the admitted inputs, the `JobHandle` when submitted |
| `strands` | live strands by id |
| `sheap` | heap of `(next key, seq, strand)`; stale entries are refreshed or dropped on read |
| `runnable` | strands to run at the next fetch (`mark_runnable`, `take_runnable`) |
| `window`, `wheap` | the `Window`; a heap of `(key, seq, inflight)` of every added inflight |
| `unfinished` | inflights not yet done (the `window` cap counts these) |
| `issued` | inflights issued and not yet done |
| `final_names`, `final_key` | the names view and key where a strand fetched `end` |
| `failure`, `record` | `{kind, node, message}` or `None`; the conformance record list when recorded |
| `stats` | `calls`, `squashed`, `retired`, `window_peak`, `strands_peak`, `inflight_peak`, `mutates_copies`, `parks`, `reused`, `bytes_produced`, `bytes_peak`; `program` (the region's semantic hash); others as they occur |
| `ended`, `paused`, `held`, `start_time` | lifecycle; `held` keeps ready inflights of a paused job |
| `steps` | instructions executed (the `step_budget` count) |
| `bytes` | estimated bytes it holds: inputs, live held slots, kept results ([admission](pool/admission.md#signals)) |
| `freed` | the dispatcher's deque of `(job, bytes)` released by freed held slots |
| `memo` | results kept for a refetch after a fault (`Memo`: entries `(inputs digest, result, bytes)` by address, their count and bytes), or `None` |
| `sampled`, `auto_key` | event sampling; a key the source or machine made (a source's item index or the process counter of keyless submits), not one given |
| `snap_at`, `snap_off` | retired count at the last snapshot; snapshots stopped after an error |
| `inputs_dig` | digest of the inputs as given, taken in `start_job` when the machine has a store; `None` without a store or when the inputs digest only by type |

| Method | Does |
|---|---|
| `add_strand(st)` | registers it, pushes it on `sheap`, updates `strands_peak`, marks it runnable |
| `end_strand(st)` | `ENDED`, removed from `strands` (its heap entry is dropped later) |
| `oldest_live_key()` | the smallest `next_key()` of a live strand, from `sheap` |
| `add(inf)` | into the window and `wheap`; updates `window_peak` |
| `issue(inf)` | `ISSUED`, `issued += 1`, updates `inflight_peak` |
| `oldest_open_key()` | the key of the oldest inflight still waiting, ready or issued, or done with an untaken fault, from `wheap` |
