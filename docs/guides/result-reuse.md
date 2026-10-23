# Execution-compatible result reuse

A matching program hash is not enough to identify the implementation that produced a
stored result. Host code, adapters, services/models and observable execution policy can
change while the KIR source remains identical. Result stores therefore use a versioned
execution namespace instead of the old program/engine-`repr` key scheme.

This is the **S2a compatibility slice**, not deterministic reuse or exactly-once execution.
It does not add logical invocation IDs or classify stochastic nodes as safe to cache.
Keep the existing `ordered`, `mutates`, store-mode and idempotency safeguards in mind.

## Declare implementation and binding revisions

```python
from kohakunode import NodeLibrary

from togeari import Machine, Use
from togeari.engines.python import PythonCallEngine
from togeari.stores import MemoryStore

lib = NodeLibrary("example_cache")


@lib.node()
def square(x: int) -> int:
    return x * x


store = MemoryStore()
with Machine(
    program="@def (x: int)main(y):\n    (x)square(y)\n",
    entry="main",
    libraries=[lib],
    bind={
        "square": Use(
            PythonCallEngine(workers=2, mode="thread"),
            store="content",
            implementation_revision="square/v1",
            binding_revision="python-square-service/v1",
        )
    },
    store=store,
    persistence_compatibility="strict",
) as machine:
    assert machine.submit({"x": 3}, key="first").result(5).outputs == {"y": 9}
    assert machine.submit({"x": 3}, key="second").result(5).outputs == {"y": 9}
```

`implementation_revision` identifies the host/node implementation only.
`binding_revision` attests the whole resolved implementation, adapter, eligible engine or
service/model and semantic configuration bundle. Both are independently included when
supplied. They are nonempty opaque strings—not automatic source-code hashes, `repr`
snapshots or proofs of determinism. Bump them when their claimed contracts change.
Maintained builtin revisions may cover known implementations; they do not magically cover
arbitrary service configuration or user code.

These optional revision fields are also accepted by `Inline`. Inline calls still execute
on the dispatcher; adding a revision does not give them a result-store binding.
Bindings are construction declarations. Mutating a `Use` object or `machine.bind` to rebind
already loaded code is unsupported; cached loading checks the frozen declaration fingerprint.

## Versioned keys and eligibility

The new result-key domain is `togeari.result`, version **2**:

| Store mode | Key contents after domain/version |
|---|---|
| `content` | mode, execution hash, node spelling, strong input digest |
| `positional` | mode, execution hash, job key, job input digest, region-relative instruction position, order key |

There is **no fallback to old result keys**. Old entries may remain in a store, but the new
machine never probes their namespace. A known execution mismatch naturally gets a different
key and executes on a cache miss. Strong-value digest requirements still apply.

The execution manifest covers the linked program/dependencies, normalized decoder layout,
signatures and declared effects, implementation/binding revisions and scoped observable
policies (including call/attempt deadlines, retry configuration and step budget). Source
origins, absolute code addresses, local epochs and replica counts are not semantic identity.
Performance-only worker count, batch capacity and equivalent replicas do not by themselves
change a key. This is a scoped contract, not a claim that every possible future semantic
configuration has already been modeled.

`persistence_compatibility` controls incomplete identity:

- **`warn`**, the migration default: permit ordinary incomplete identity with a visible
  warning and `JobRecord.stats["persistence"]` marked `unverified`.
- **`strict`**: reject incomplete persisted reuse as a diagnosed cache miss, and execute the
  node normally. The result is not persisted under the unverifiable namespace either.
- Both modes reject unsupported mixed configuration history. Warnings do not authorize a
  known mismatch or a job that crossed incompatible live configurations.

The decision includes `status`, `allow` and structured `issues`. Result guards cache this
small decision for an unchanged current/admission hash, epoch and compatibility mode;
`_persistence_context` in job stats is internal primitive bookkeeping, not a stable public
identity or an authorization to skip validation of external snapshots. Expected eligibility
denials are not node failures. Store access failures remain ordinary store errors. Snapshot
compatibility uses the same identity concepts but has different restart safety rules; an
incompatible checkpoint is not an invitation to silently replay effectful work from scratch.

**Positional reuse remains position-based recovery.** It is not invocation-safe across
logical program changes and does not establish exactly-once external effects. Invocation
identity and explicit deterministic/reuse eligibility are separate later work.

## Live configuration changes and job history

Each admitted job records an execution manifest and a process-local `execution_epoch`.
Observable lane-policy changes and supported unit attach/detach transitions refresh the live
manifest and advance affected regions' local epochs. Timeout, retry maximum/backoff and
lane-dead limits are observed, including change-then-revert edits through their setters.
Epochs are bookkeeping, never inputs to the portable execution hash.

A job that spans a transition is denied persisted reuse and writes in **both modes**.
Compatibility is checked before lookup and again before storing a completed result, so an
in-flight call cannot populate a stale key after the machine changes configuration.
Earlier results already committed to the store are not retroactively removed.

Future jobs are not permanently poisoned by an older job's mixed history: they receive a
fresh admission epoch. Known policy changes produce a new execution identity. Changes to
eligible units invalidate the original bundle attestation; the current manifest remains
incomplete until explicitly reconstructed with a binding declaration covering that bundle.
Such fresh jobs may use `warn` unverified eligibility, while `strict` still refuses reuse.
Construct a new machine/region with an appropriate updated bundle revision to re-attest it;
editing the old binding declaration in place is not supported.

Draining and detached units cease to be eligible implementations; temporarily down units
remain part of the configured implementation set. Arbitrary direct list surgery on
`lane.units` is not an attach/detach protocol. Current membership is checked at persistence
boundaries, but unsupported mutation-and-reversion outside the supported hooks is not an
execution-history API.

## Diagnostics and testing

Result eligibility uses the same Python helper from both dispatcher implementations;
native store keys use the same versioned digest domain. Tests cover revision changes,
legacy-key isolation, warn/strict incomplete identity, performance-only settings, changes
while results are in flight, setter reversion, draining detach, and later fresh jobs after
a transition. They use fixed-delay scripted engines and virtual time rather than scheduling
sleeps.
