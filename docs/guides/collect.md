# Ordered result collection

`collect` runs an `each`-style body and returns one value per input, in input order:

```kir
@def (items: list)main(results):
    (items)collect(item, index, `body`, value -> results)
    body:
        (item, index)add(value)
```

The index output is optional: ``(items)collect(item, `body`, value -> results)``.
The input is eagerly materialized once before any body runs. An empty input produces `[]`.
The yield expression is evaluated at each normal body end, after its selected branches and
nested bodies. It may be a name or literal. The resulting list is assigned to the target
only when every iteration completes successfully.

Run the nested worker example with `python examples/collect/machine.py`; it checks replay
and re-execution on three jobs, including empty rows and an empty outer collection.

The body has `each`'s flat name scope. Carried state remains sequential even when independent
work overlaps:

```kir
@def (items: list)main(results, total):
    total = 0
    (items)collect(item, `body`, total -> results)
    body:
        (total, item)add(total)
```

For `[1, 2, 3]`, this produces `results=[1, 3, 6]` and `total=6`. The target `results`
is inaccessible inside the body, including nested references and writes. Jumps may not
escape the collection body. Nested collections are supported; each invocation has its own
private accumulator and output slot.

## Execution and precise faults

Togeari's default passes leave `collect` native. Both cores reuse lazy iteration spawning,
carried-variable phis and ordinary ready-call scheduling. Engines see only the body's host
calls, not an accumulator operation or a batch-specific record format.

An iteration ending at fetch is not a committed result: its output can still be pending,
and an older operation can still fail. Internal capture markers retain each yielded value
only when it retires in program order. A final marker publishes the list and wakes its
consumers after all captures retire. Faster later iterations cannot reorder the result.
Private accumulation is O(n) reference storage; it does not repeatedly copy growing lists.
The private reference buffer and retained values count toward estimated held job bytes.

A failure escaping the body prevents result assignment. An earlier target binding remains
visible to a surrounding handler; no partial list is exposed. Earlier body assignments and
ordered effects are not rolled back. A failure caught inside the body can recover normally
and contribute its eventual yield. Nested collectors abandoned by an escaping fault are
discarded; surrounding collectors whose bodies recover continue.

Input snapshot errors are ordinary catchable failures with node `kir.collect_items` and
the original exception kind/message. For native control execution they do not introduce a
synthetic call-record entry. Existing `each` input-error behavior is unchanged.

## Initial mutation restriction

Collection retains references, like appending a value to a list in the sequential
interpreter. It does **not** silently deep-copy yielded values. Repeated yields of the same
mutable object retain that object; immutable records and nested result lists work normally.

To keep this contract correct with current shallow mutation-version tracking, KIR rejects
programs that contain `collect` and any resolved host call declaring `mutates`. The restriction
covers definitions, imported helpers, callers and code outside the collection—not just its
body. Functional operations such as `kir.std.append` (which returns a new outer list), record
`with` operations and ordinary assignments remain allowed. Ordered nonmutating operations
are allowed. Existing programs using only `each` are unaffected.

This is deliberately conservative. Undeclared mutation, background mutation of returned
values and external modification of admitted inputs are not made safe by collection.
Supporting declared mutations alongside arbitrary retained container aliases requires a
broader ownership/versioning design, not a change from reference to snapshot semantics.

## Fallback lowering and replay

A machine may explicitly include `expand_collect` before `inline`:

```python
from togeari import Inline, Machine

program = """@def (items: list)main(results):
    (items)collect(item, `body`, value -> results)
    body:
        (item, 2)mul(value)
"""

with Machine(
    program=program,
    entry="main",
    bind={"kir.std.*": Inline()},
    passes=["order_dataflow", "expand_collect", "inline", "strip_meta"],
    record=1.0,
) as machine:
    assert machine.submit({"items": [2, 3]}).result(5).outputs == {"results": [4, 6]}
```

The fallback uses reserved internal `kir.collect_items` and `kir.collect_append` helpers,
a private accumulator and final assignment. Those exact builtin helpers bind inline
automatically; an unrelated user node cannot shadow them. Fresh-list append copies its outer
list, so this baseline can do quadratic reference copying. Prefer native collection unless
explicit lowering is needed.

For fallback conformance, `JobProgram.written` receives `expand_collect` only, retaining entry
definitions. Runtime execution still applies the full requested passes. Consequently both
sides record the same administrative helper calls. Native collection records only ordinary
body calls. Archives retain the linked original text and selected passes, so rebuilding
recreates the appropriate replay representation and restores its source-origin sidecars.

## Observability and snapshots

Job statistics expose these native-collection counters when used:

| Counter | Meaning |
|---|---|
| `collect_captures` | values captured at retirement, including committed prefixes later abandoned by a fault |
| `collect_finalizations` | result lists successfully published |
| `collect_peak_items` | largest private accumulator length reached by any one collection |

These counters support structural scaling tests without wall-clock assertions. They are
not counts of engine calls, and internal markers do not add conformance call records.
Instruction spans retain the collection's source origin, including its generated iteration
end; ordinary body-call origins remain unchanged.

Snapshots are intentionally unavailable while a native collector is active. Existing
snapshot boundaries before or after collection remain usable. The initial implementation
does not serialize a partial collector into the old snapshot format or claim general
run-ahead checkpoint support. This conservative restriction does not change job execution.
