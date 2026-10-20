# Binding

`bind_region` in `src/togeari/core/program/bind.py` resolves every `call` instruction of a region
to a `Bound` and stores it in `Instr.binding`. It runs for each region at construction and for
each region loaded at run time.

## Binding values

| Class | Fields | Meaning |
|---|---|---|
| `Use` | `target`, `adapter=None`, `store=None`, `idempotent=False` | run on a lane (`target` a `Lane`, or an engine that becomes a one-unit lane through `Machine.lane_of`) |
| `Inline` | `warn_us=None` | run the node's Python function on the dispatcher |
| `Bound` | `pattern`, `inline`, `fn`, `lane`, `adapter`, `store`, `idempotent`, `warn_us` | the resolved binding of one instruction |

A `bind` value that is neither `Use` nor `Inline` is wrapped as `Use(value)`.

## Pattern resolution

- `names_of(ins, region)`: the node name as written, plus `kir.std.<name>` when the node resolves
  to the standard library.
- `match(patterns, names)`: every pattern that `fnmatch.fnmatchcase`-matches one of the names,
  sorted by `_specificity(pattern) = (0 if it has a wildcard else 1, length without * and ?)`.
  The best pattern wins; patterns of equal specificity are a tie.

| Node | Patterns | Result |
|---|---|---|
| `d.lookup` | `d.lookup`, `d.*` | `d.lookup` (exact beats glob) |
| `d.lookup` | `d.look*`, `d.*` | `d.look*` (longer glob) |
| `d.lookup` | `d.*`, `*.l*` | tie (both strip to 2 characters): problem |
| `len` (std) | `kir.std.*` | `kir.std.*` |

## Checks

Every problem is added to the construction `Problems`.

| Check | Problem text |
|---|---|
| node has no signature | `node 'x' is unknown` |
| no pattern matches | `node 'x' is not bound` |
| tie | `node 'x' matches 'a' and 'b' equally` |
| `Inline` without a library function | `node 'x' is inline but has no Python implementation` |
| adapter check | each message of `adapter.check(signature)` |
| unit protocol | `unit 'u' speaks 'p'; the adapter of 'x' speaks [...]` when the unit's protocol is not in `adapter.protocols` |
| `mutates` on a copying unit | when `unit.shares_memory` is false and a mutated port is not a signature output |
| store mode | `store=...` not in `STORE_MODES` (`None`, `"content"`, `"positional"`) |
| `ordered` with a store | warning; the store mode is dropped |

The unit checks are `check_unit(ins, adapter, unit, protocol)`, the same function
`Machine.attach_now` runs for a unit attached while running. `unit_protocol(unit, default)` is the
unit's `protocol`, or the machine's default protocol when the unit has none.

Matched patterns are added to `used`; `Machine` warns about patterns no node used.

## Adapter

`bind_region` takes `Defaults(adapter, protocol)` from `Machine(adapter=, protocol=)`
(`PythonAdapter()` and `"python"` unless given). `adapter_for(given, default, fn)` picks the
given adapter or the default; an adapter with a `bound` method is bound to the node's library
function when it is the default or has no function yet. `PythonAdapter.prepare` returns
`PythonCall(fn, inputs)`; `decode` returns the raw reply unchanged, the node result as the
function returns it (`src/togeari/core/contracts/adapter.py`). See
[writing an engine](../../guides/writing-an-engine.md) for the adapter contract.
