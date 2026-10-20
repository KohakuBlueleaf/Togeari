# Run-ahead

A `branch`, `switch` or `each` whose operand is still pending does not stop the job. The strand
parks at it, and a continuation strand starts at the reconvergence point and reads phis for every
name the region may write. Nothing runs on a path the sequential run would not take.
Code: `FrontEnd.control_value`, `park`, `enter_region`, `make_join`, `resolve_join`,
`early_resolve` of `ControlOps` in `src/togeari/core/runtime/frontend/control.py`.

## Deciding

`control_value(st, ins)` reads `effective(slot_of(operand), st.next_key())`.

| Operand | Condition | Result |
|---|---|---|
| ready | — | the value; the instruction proceeds |
| pending | `st.resume != "parked"`, `knobs["run_ahead"]`, `tables.structured(addr)`, job not paused, not inside a frame this instruction opened (`own_frame`) | `park` |
| pending | the strand already parked here | stays `PARKED`, waits on the slot |
| pending | otherwise | `STALLED`, `resume = "stalled"`, waits on the slot; no key consumed |

A woken strand re-runs the same instruction. A stalled strand then takes its key in
`enter_region`, so its keys equal those of a strand that never waited.

## Parking

`park(st, ins, slot)`:

1. `key = consume(st)` (the branch's key).
2. `join = make_join(names, may_write(addr), region_writes(addr))`: a fresh phi slot per name in
   `may_write`; when the set holds `ALL`, a phi for every bound name as well.
3. A continuation strand at `reconv[addr]` with the strand's prefix and counter (the key after
   the branch), the same frames tuple, and `NameMap.of(names, phis)`. `job.add_strand` marks it
   runnable, and it runs in the same `advance`.
4. The parked strand pushes `Frame(reconv, saved=(prefix, counter), join, branch=addr)`, moves to
   prefix `(*key, 1)` counter 0, becomes `PARKED` with `resume = "parked"`, counts
   `stats["parks"]`, waits on the operand slot, and emits `strand.park` (with the continuation's
   id).

`enter_region(st, ins, target)` takes a decided arm. For a parked strand it only sets
`pc = target`: the frame and key prefix already exist. Back inside the frame the instruction
opened (a loop's exit branch on the next iteration), it closes the frames above that one, takes
the key there and pushes nothing. Otherwise it consumes the key and, for a structured
instruction, pushes a frame without a join and moves to `(*key, 1)`.

A loop whose exit condition is pending therefore parks once: the continuation waits at the
loop's end through the phis of that one join, the parked strand runs every further iteration
inside it (stalling, not parking, on later pending conditions), and the join resolves when the
strand leaves the loop.

## Phis

A phi is a `Slot(phi=True)`. Readers wait on it like any pending slot; `resolve_phi` forwards it
to the slot the arm wrote ([dispatcher cycle](../loop/cycle.md)).

- **Early resolution.** After each instruction of a strand inside a join frame,
  `early_resolve` reads `join.table[pc]`, the names still writable before `reconv`. Every phi
  whose name is not in that set resolves to the strand's current slot for the name. A set holding
  `ALL` resolves nothing.
- **At the reconvergence point.** `pop_frames` pops the join frame; `resolve_join` resolves every
  open phi to the strand's current slot, and the parked strand ends.
- A name no path wrote resolves to a fresh `undefined()` slot holding `UNDEF`; any `mutates`
  versions the phi carried move onto that slot, which belongs to this job only.

## Example

```
(a)slow(c)
(c)branch(`yes`, `no`)
yes:
    (a)inc(t)
no:
    t = 0
(a)inc(u)
(t, u)mix(v)
```

With `c` pending: the strand parks at the `branch` (key `(1,)`). The continuation strand fetches
`(a)inc(u)` at `(2,)`, which issues at once, and `(t, u)mix(v)` at `(3,)`, whose `t` is a phi.
When `c` resolves true, the parked strand fetches `(a)inc(t)` at `(1, 1, 0)` and the phi `t`
forwards to its output slot; `mix` issues when `inc` completes.

## Limits

- Only structured instructions park (`Tables.structured`). An unstructured branch, and every
  branch with `run_ahead=False`, stalls.
- A paused job stalls instead of parking.
- No statement of an undecided arm is fetched, so nothing runs on a path the sequential run
  might not take.
