# Engines and lanes

## Engines

The machine defines what an engine provides and how it behaves:

- `submit` never blocks;
- exactly one result per item;
- capacity is honest;
- `ItemError` (this item failed) is kept apart from `UnitError` (the whole unit is blocked).

| Contract | One submit | Who queues and batches |
|---|---|---|
| `CallEngine` | one item | the lane queues |
| `BatchEngine` | a list of items with equal `key()` | the lane queues and forms batches |
| `StreamEngine` | one item | the engine queues and batches internally |

How and where an engine runs is the engine's business. Builtins: `PythonCallEngine` (worker
threads or processes), `WebCallEngine` (any HTTP service), a batched device engine, and the
simulated `SimCall` / `SimBatch` / `SimStream` for checks.

## Lanes

A lane holds one or more interchangeable units of one kind. It:

- queues ready statements in priority order;
- forms batches for batch units (a former decides how many and how long to wait);
- chooses units (a selector) and multi-issues to every unit with free capacity;
- retries items on unit errors, after a backoff, and fails over to other units;
- times items out;
- keeps a unit down until its `ready()` says otherwise, and attaches or detaches units while
  the machine runs.

Nothing polls: engines answer through `done`, report when capacity rises without an answer,
and every timed action (a batch wait, a retry backoff, a timeout, a health poll) is a deadline
the dispatcher sleeps until.

## Bindings

Every called node is bound by name pattern: `Use(lane)` sends it to a lane (an adapter turns
inputs into the engine's payload and back), `Inline()` runs it on the dispatcher. Construction
checks every binding, lane and unit at once and reports every problem in one
`ConstructionError`.
