# Fetch and order keys

`FrontEnd` in `src/togeari/core/runtime/frontend/fetch.py` walks strands through code memory
in program order, renames writes to fresh slots, and gives every instruction an order key. It
mixes in `ControlOps` (`frontend/control.py`, [run-ahead](run-ahead.md)) and `EachOps`
(`frontend/each.py`, [each](each.md)); `FrontEnd.ops` maps each `Op` to its method. KIR rules
shared with the back end (`UNDEF`, `undefined()`, `RuntimeFault`, `fresh`) are in
`src/togeari/core/runtime/kir.py`.

## Driving a job

`advance(job)` takes the job's runnable strands into a heap keyed by `(next_key(), id)` and,
while the heap is non-empty, the budget lasts and the job has not ended:

1. Pop the oldest strand; skip it unless `RUNNABLE`.
2. `run(strand, budget)`; subtract the instructions it ran.
3. Still runnable: into the `capped` heap when `blocked_by_caps` holds, else back into the heap.
4. Strands made runnable meanwhile join the heap; the oldest capped strand rejoins when it is no
   longer blocked.

Strands left in either heap stay runnable for the next cycle; a budget stop marks the job
dirty again. `FETCH_BUDGET = 20_000` instructions per `advance` bounds a job whose loop never
reaches a call.

`run(st, budget)`: a spawner strand starts one iteration (`spawn_next`) unless capped. Any other
strand loops while `RUNNABLE`:

1. Stop when `blocked_by_caps(st)` ([window caps](../retire/window-and-retire.md)).
2. `pop_frames(st)`: while the innermost frame's `reconv` equals `pc`, pop it. A frame with a
   join resolves the join and ends the strand; otherwise `(prefix, counter) = frame.saved`.
3. `job.steps += 1`; past `knobs["step_budget"]` the step is `RuntimeFault("more than N steps")`.
4. Run `ops[instr.op](st, ins)`. A `RuntimeFault` becomes a control fault
   ([faults](../retire/faults.md)) and stops the strand.
5. While still runnable, `early_resolve(st)` ([run-ahead](run-ahead.md)).

`end(st)` ends a strand (`job.end_strand`) and emits `strand.end`.

## Instructions

`consume(st)` returns `(*prefix, counter)` and increments `counter`. Every instruction a strand
executes consumes exactly one key, whatever happens at run time; waiting on a control operand
consumes none. An eager iteration strand ends at `iter_end` in `pop_frames` without executing it.

| `op` | Fetch does |
|---|---|
| `call` | key; `Inflight` with `inputs = [(port, slot_of(operand))]`, `snapshot = snapshot_of(st)`; a version slot per `mutates` port ([effects](../back-end/effects.md)); a fresh `PENDING` slot per named output, set in `names`; `pc = next`; `Dispatcher.admit_inflight` |
| `assign` | key; `names[target] = slot_of(operand)` (an alias for a name, a constant slot for a literal); no inflight |
| `jump` | key; `pc = target` |
| `branch`, `switch` | ready operand: key and enter the chosen arm; pending operand: park or stall ([run-ahead](run-ahead.md)) |
| `each` | [each](each.md) |
| `iter_end` | key; next in-order iteration, or leave the loop ([each](each.md)) |
| `end` | key; `job.final_names` (a names view), `job.final_key`; the strand ends |

`slot_of(st, operand)`:

| Operand | Slot |
|---|---|
| `("n", name)` | the name's current slot; an unbound name is `RuntimeFault("'x' is not defined")` |
| `("l", v)`, `("d", v)` | `const(fresh(v))`; `fresh` deep-copies list, dict and set literals per use |
| `("missing", port)` | `RuntimeFault("input 'port' missing")` |

A branch condition must be a `bool`; a `switch` with no matching case and no default is a
`RuntimeFault`. A name whose phi resolved to "never written" holds the `UNDEF` sentinel; reading
it as a control operand or a call input is a `RuntimeFault`.

## Order keys

Keys are tuples compared lexicographically. They give one total order per job that equals the
sequential order, so the window, retire, squash and gates all compare keys.

| Situation | Keys |
|---|---|
| straight-line code | `(*prefix, n)`, `(*prefix, n + 1)`, ... |
| structured `branch` / `switch` at `(*prefix, n)` | arm under `(*prefix, n, 1)` from counter 0; after the reconvergence point `(*prefix, n + 1)` onward |
| unstructured `branch` / `switch` at `(*prefix, n)` | no frame; the arm continues `(*prefix, n + 1)` |
| a `branch` / `switch` reached again inside the frame it opened (a loop written with `jump`) | no new frame; frames above it close, and the strand keeps numbering inside its arm, after every key those frames used |
| `each` at `(*prefix, n)` | iteration `i` under `(*prefix, n, i)` from counter 0; after the loop `(*prefix, n + 1)` |

A loop's exit branch has its region around the whole loop, so every iteration runs inside the
one frame the first pass opened: keys stay `(*prefix, n, 1, m)` with `m` growing, and the frames
tuple does not grow with the iteration count. `own_frame(st, addr)` finds that frame when only
unparked branch frames lie above it; `reenter(st, i)` closes those and takes the counter after
the innermost one's saved key.

A strand that parks gets the same keys as one that does not: parking consumes the branch's key
and the continuation strand starts at the counter after it. The spawner of an eager `each` sits
at `(*prefix, n, i)` before it starts iteration `i`, so it orders after iteration `i - 1` has
started and before any statement of iteration `i`.

For the program in [code memory](../../program/code-memory.md), with `c` true and two items:

| Key | Address | Statement |
|---|---|---|
| `(0,)` | 7 | `(a)small(c)` |
| `(1,)` | 6 | `branch` |
| `(1, 1, 0)` | 9 | `(a)inc(t)` |
| `(2,)` | 5 | `(a)inc(u)` |
| `(3,)` | 4 | `acc = []` |
| `(4,)` | 3 | `each` |
| `(4, 0, 0)`, `(4, 0, 1)` | 12, 11 | iteration 0: `mix`, `append` |
| `(4, 1, 0)`, `(4, 1, 1)` | 12, 11 | iteration 1: `mix`, `append` |
| `(5,)` | 1 | `(u)flaky(r)` |
| `(6,)` | 0 | `end` |

If `flaky` fails, the handler `r = 99` (address 2) takes key `(5,)` again: the faulted inflight
has left the window, and the handler strand restarts from the faulting statement's
`(prefix, counter)`.
