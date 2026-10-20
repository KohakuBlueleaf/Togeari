# Decode tables

`Tables` (`src/togeari/core/program/decode/analysis.py`) holds the static facts the front end
needs about one region: where control paths rejoin, and which names a path may still write.
`CodeMemory.load` builds one per region from the region's addresses and the compiler's
`each_body`.

## Reconvergence: `reconv`

`ipdoms(mem, addrs)` computes the immediate post-dominator of every address with the
Cooper-Harvey-Kennedy algorithm on the reverse flow graph, rooted at the virtual exit `EXIT = -1`.
Edges are `Instr.succ()` only; fault edges are not part of the graph, because a taken fault
squashes everything younger and continues at a handler (see [faults](../runtime/retire/faults.md)).

`Tables.reconv[addr]` is that post-dominator: for a `branch` or `switch` the address where its
arms rejoin, for an `each` the address after the loop, for a loop's exit `branch` the statement
after the loop. Often it is the region's `end` instruction.

## `structured(addr)`

True when `reconv[addr]` exists, is not `EXIT`, and, for an instruction inside an `each` body
(`each_ctx` set), lies inside that same body (`each_body[each_ctx]`, which includes the body's
`iter_end`). Only structured control instructions open a key frame and may park
([run-ahead](../runtime/front-end/run-ahead.md)); an unstructured one stalls its strand until
the operand is ready.

## May-write tables

`write_table(mem, start, stop)` collects the addresses reachable from `start` without passing
`stop` (`region_nodes`) and, by a fixpoint over successors, maps each address to the names
writable from it before `stop`.

| Method | Start, stop | Gives |
|---|---|---|
| `region_writes(a)` | `succ(a)`, `reconv[a]` | `{addr: names writable from addr before reconv}`; for an `each` also its `item` / `index` names, and an entry for `a` itself holding the union |
| `may_write(a)` | — | `branch` / `switch`: union of `region_writes(a)` over its successors; `each`: `region_writes(a)[a]` |
| `body_writes(e)` | `[mem[e].body]`, `mem[e].iter_end` | `{addr: names writable from addr before iter_end}` for an `each` body |
| `body_may_write(e)` | — | `body_writes(e)[body]`: every name an iteration may write |
| `carried(e)` | — | the body writes live at the body entry (below) |

The tables are built lazily per control instruction and cached in `_writes` (`body_writes` under
the key `-e - 2`) and `_carried`. A set holding `ALL` (`"*"`) means the region calls a `mutates`
node; the front end then treats every name as possibly written (`has_all`).

## Carried names

`carried(e)` is `body_may_write(e) & live_in(mem, body, iter_end, body_may_write(e))`.
`live_in` is a backward liveness fixpoint over the body, where reaching `iter_end` counts as a
read of every body write. A body write is therefore carried when some path from the body entry
reads it before writing it, or leaves it unwritten until `iter_end` (its value then flows to
the next iteration and out of the loop). With `ALL` in the body writes, `carried(e)` is the body
writes including `ALL`.

| Front-end use | Table |
|---|---|
| phis of a parked branch, switch or `each` | `may_write(a)` |
| early phi resolution while the parked strand walks its arm | `region_writes(a)[pc]` |
| phis between eager `each` iterations | `carried(e)` |
| phis of the last iteration (the names after the loop) | `body_may_write(e)` |
| early resolution inside an iteration | `body_writes(e)[pc]` |

## `has_exit`

Set on each `each` instruction when building `Tables`: true when an instruction of the body
(other than `iter_end`) has a successor outside the body and different from the `each` itself,
i.e. a `jump` leaves the loop. `has_exit` selects in-order iterations over eager strands
([each](../runtime/front-end/each.md)).

## Example

For the program in [code memory](code-memory.md):

| Address | Instruction | `reconv` | `structured` | `may_write` |
|---|---|---|---|---|
| 6 | `branch c ? 9 : 10` | 5 | yes | `{t}` |
| 3 | `each xs as (x, None)` | 1 | yes | `{acc, v, x}` |

`body_may_write(3) = {acc, v}` and `carried(3) = {acc}`: `append` reads `acc` before writing
it, while `v` is written by `mix` on every path before any read. Iteration `i + 1` reads `acc`
through a phi of iteration `i`; the code after the loop reads both `acc` and `v` through phis of
the last iteration.
