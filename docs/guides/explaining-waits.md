# Explaining waits

The opt-in `waits` query explains the causal evidence currently visible to the
dispatcher. It is a metadata snapshot, not a deadlock detector, a scheduling
prediction, or a new execution policy. No UI or tracing subscription is required.

```python
snapshot = machine.inspect("waits", job=resident_job_id, limit=256).result(5)
# Omit job to inspect all resident jobs within the same total bound.
```

Use the numeric resident job ID from `inspect("jobs")`, not a source key or a
`job_execution_id`. Unknown jobs raise a query error. `limit` is an exact integer
from 1 to 10,000 (default 256) and caps **entities plus relations together**.
The dispatcher answers between cycles; plugins may use the query directly only
on the dispatcher thread or while the dispatcher is stopped.

## Reading a snapshot

The version-1 result has `schema`, `run_id`, `snapshot_id`, `job`, `entities`,
`relations`, `truncated`, and `unsupported`. An edge points **from the waiting
entity to what it waits on**. Follow an inflight's input edge to a pending slot,
then the slot's producer edge to materialized work. A pending phi may instead
point to the actual strand owning its unresolved join. A not-yet-materialized
control step remains a strand at its current PC; the query never invents a call.

Each entity has a typed, machine-lifetime-local ID. Slots, inflights and strands use
existing runtime IDs rather than a source node ID, human job key or payload hash.
`run_id` namespaces them for the machine object's lifetime; it is **not** a
Recorder/RunReport run ID (one machine may process multiple sources/runs).
`snapshot_id` increases for each query of that machine.
These IDs are not persistence handles and expire across restore/restart. Dynamic
invocation/attempt identities are included separately when available.

Instruction entities carry their source origin verbatim: module, node ID, line,
definition, call ancestry and generated attribution retain their existing
meanings. Multiple calls at one source location stay separate dynamic entities.
Infrastructure entities have `origin: null`; they must not be attributed to a
nearby source statement merely for presentation.

## Reasons and limits

| Reason | Evidence |
|---|---|
| `input` | the effective input slot after phi forwarding and older live mutation versions, or its materialized producer |
| `control` | an unresolved control operand or actual join/collection owner strand |
| `effect` | an ordered call, or mutating call under wait policy, behind older live work/strands |
| `window` | a strand actually held by window/retire-buffer caps, respecting the oldest-work progress exception |
| `lane_capacity` | cached lane max-inflight or no-unit-up evidence, not a fresh engine probe |
| `batch_wait` | a queued item whose cached batch deadline is in the future or absent |
| `retry_backoff` | an actual retry-held item with a future retry time |
| `checkpoint` | an active cut and its actual frontier/held/draining phase, never an ordinary pause inferred as a checkpoint |
| `resource_budget` | a queued item's actual cached `budget_prefix` selection marker; an estimate or high total alone does not establish a wait |
| `unknown` | current metadata does not establish a more precise blocker |

Elapsed/backoff details may change between queries without changing a causal
relation ID. Relation IDs depend on endpoints, reason and stable port/gate—not
current remaining time or counters. Inspecting a wait does not run `payload()`,
prepare adapters, call selectors/formers, ask an engine for capacity, or tidy
scheduler heaps. Custom lane inspection overrides are not invoked; unsupported
metadata is reported explicitly.

Resource costs are cached estimates, not physical memory reservations. A null
resource `wait_reason` is not permission to claim the budget is full. Missing
resource dimensions or unavailable policy evidence remain unsupported/unknown.
Terminal `BudgetBlocked`, oversized-resource rejection, and completed faults are
outcomes, not waits.

If bounded collection omits state, `truncated` is true. When room remains under
the total bound, a summary entity explains the omission. All returned relations
reference returned entities; missing producers are not replaced with imaginary tasks. `unsupported` lists
unavailable metadata capabilities, including those missing in an older native
extension. A large job may need a larger limit or a job-specific query. A wait
snapshot alone cannot prove that no other blocker exists or that the machine
will make progress under an unbounded arrival stream.
