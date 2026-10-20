# `each`

`EachOps` in `src/togeari/core/runtime/frontend/each.py` runs a loop body either as one strand
per item, started lazily by a spawner (eager), or along one strand's program counter (in order).

## Start

`op_each(st, ins)`:

1. `control_value` reads the items. A pending items slot parks like a branch
   ([run-ahead](run-ahead.md)); the phis cover `may_write(each)`, which includes the body's writes
   and the `item` / `index` names.
2. `items = list(value)`; a non-iterable value is a `RuntimeFault`.
3. The `each`'s own key: consumed now, or, after a park, recovered from the parked frame's
   `saved` counter minus one.
4. No items: `pc = after`; names are unchanged.
5. `ins.has_exit` ([decode tables](../../program/decode-tables.md)) chooses the mode.

| Mode | When | Iterations |
|---|---|---|
| eager (`each_at_once`) | the body has no exit | one strand per item, started in index order by a spawner strand |
| in order (`each_in_order`) | a `jump` can leave the body | one after another on the same strand |

## Eager iterations

`each_at_once(st, ins, items, key)`:

- `last`: a `Join` with a phi per name in `body_may_write(each)` (the names the code after the
  loop reads from the last iteration).
- `carried`: `tables.carried(each)`, the names an iteration reads from the one before. With
  `ALL` in the body writes, both sets are every name bound at the `each`, the body writes and
  `item` / `index`.
- An `EachSpawn(ins, items, key, base, saved, frames, carried, table, last)` holds what each
  new iteration needs; a **spawner** strand `Strand(job, each addr, prefix=key, counter=0,
  frames, names=None, spawn=e)` is added to the job.
- The `each` strand itself continues at `after` with the names at the `each`, overlaid with
  `last.phis`, and `item` / `index` bound to the last item and index: the **live-out** names.
  A strand that parked on the items reaches `after` as its reconvergence point instead: its park
  join resolves to these names and it ends.

`spawn_next(spawner)` starts iteration `i = spawner.counter`:

- names: the names at the `each`, overlaid with the previous iteration's phis (`e.prev`), with
  `item` / `index` bound to `const(items[i])`, `const(i)`;
- join: `last` for the final item, else a new `Join` over `carried` (table
  `body_writes(each)`);
- the strand starts at `body` with prefix `(*key, i)`, counter 0, and frames
  `(*frames, Frame(iter_end, saved, join, each, "iter", each, items, i, key))`;
- `e.prev = join`; `spawner.counter = i + 1`; after the last item the spawner ends.

The spawner's `next_key()` is `(*key, i)`, so `advance` runs iteration `i - 1` (keys
`(*key, i - 1, ...)`) before starting iteration `i`. At a window cap the spawner waits like any
younger strand, and even as the job's oldest live strand it waits while an older inflight is
unfinished (`oldest_open_key`), because that work will free the window
([window caps](../retire/window-and-retire.md)). An `each` over many items therefore holds at
most a window's worth of iterations at once.

An iteration strand resolves its phis early as it passes the last possible write of a name
(`early_resolve` on `body_writes`), and resolves the rest when it reaches `iter_end`, where it
ends. A statement that reads no carried name never waits on another iteration.

```
(xs)each(x, `body`)
body:
    (x)score(s)
    (acc, s)append(acc)
```

`score` at `(n, i, 0)` reads only `x`, so every started iteration's `score` is ready at once.
`append` at `(n, i, 1)` reads `acc`, a carried name, through iteration `i - 1`'s phi, so the
appends run in index order.

## In-order iterations

`each_in_order(st, ins, items, key)` pushes an `"iter"` frame (`reconv = reconv[each]`,
`saved = (prefix, counter)`, `items`, `i = 0`, `base = key`), moves to prefix `(*key, 0)`, binds
item 0 and jumps to `body`.

`op_iter_end` (the strand reaches the body's end) consumes a key, then:

- next item: the frame replaced with `i + 1`, prefix `(*base, i + 1)`, counter 0, `item` /
  `index` bound, `pc = body`;
- no item left: the frame popped, `saved` restored, `pc = after`.

A `jump` out of the body moves `pc` past the loop; the `"iter"` frame stays until `pc` reaches
the `each`'s reconvergence point, where `pop_frames` restores the outer key. Branches inside an
iteration still park when they are structured within the body.

## Nesting and faults

- Iteration strands share the enclosing frames tuple, so nested `each` and branches inside a
  body keep independent counters.
- A fault in an eager iteration squashes every younger strand, including the spawner and the
  iterations it started. When a `@try` inside the body catches it, the handler strand keeps the
  `"iter"` frame (join dropped, `reconv` set to the `each`'s reconvergence point) and runs the
  remaining iterations in order through `op_iter_end`. When the `@try` encloses the whole
  `each`, the frame is dropped and the handler runs after it ([faults](../retire/faults.md)).
