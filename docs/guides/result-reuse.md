# Execution-compatible result reuse

A matching program hash is not enough to identify the implementation that produced a
stored result. Host code, adapters, services/models and observable execution policy can
change while the KIR source remains identical. Result stores therefore use a versioned
execution namespace instead of the old program/engine-`repr` key scheme.

Reuse combines three separate decisions: compatible execution identity, logical
invocation recovery, and an explicit determinism declaration for content/squash reuse.
None guarantees exactly-once external effects. The existing `ordered`, `mutates`,
store-mode and idempotency safeguards remain in force.

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
            deterministic=True,
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

## Explicit reuse eligibility

`Use` and `Inline` accept `deterministic: bool | None`. This is independent of
`implementation_revision` and `binding_revision`: knowing which implementation
ran does not establish whether repeating equal inputs produces equal outputs.

- `True` explicitly authorizes deterministic content/squash reuse, still subject
  to ordered/mutation safeguards and execution compatibility.
- `False` disables that reuse. Combining it with `store="content"` is a construction
  error rather than silently overriding the declaration.
- `None` recognizes maintained builtin implementations; arbitrary host calls are
  not implicitly deterministic. Legacy `store="content"` without an explicit flag
  remains a warned determinism assertion for migration. Set the flag explicitly,
  or remove content reuse for sampling nodes.

Revision attestations and `idempotent=True` do not replace this declaration.
Inspection reports each resolved call's `reuse: {deterministic, source}`, where
source is `binding`, `builtin`, `legacy_content`, or `none`. The execution manifest
also includes effective reuse eligibility. Diagnostic provenance stays separate.

**All result-store modes on directly mutating or ordered calls are ignored with
a construction warning**, including `store="positional"`. A stored return value
alone cannot reconstruct a mutation's outcome; this increment introduces no
general mutation-replay envelope. Even an explicit determinism or revision
attestation does not waive this restriction. Manifest positional eligibility is
`not ordered and not mutates`, while content/squash also require determinism.
A nonmutating producer can still use positional recovery when a later consumer
mutates its result: recovery preserves the producer's stored pre-mutation value,
subject to snapshot/identity checks.

## Versioned keys and eligibility

The result-key domain is `togeari.result`; content remains version **2**, while
invocation-safe positional recovery uses version **3**:

| Store mode | Key contents after domain/version |
|---|---|
| `content` (v2) | mode, execution hash, node spelling, strong input digest |
| `positional` (v3) | mode, execution hash, invocation ID |

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

**Positional reuse recovers the same logical invocation**, not an equal-input sample.
Each fresh job receives a fresh `job_execution_id`, even if source key and inputs
are unchanged. Consequently, rerunning a completed job under the same key does
not hit its old positional result. A compatible version-3 snapshot restores the
same job execution ID, allowing the same invocation's committed positional result
to be recovered. Old v2 positional keys are never probed as a fallback.

Invocation IDs incorporate job execution identity, program/execution hashes,
region-relative callsite and logical control/iteration position. Actual execution
or submission attempts get distinct attempt IDs; retries retain the invocation
and append attempts. Store hits and deterministic squash reuse allocate no new
attempt. These tokens do not turn effectful services into exactly-once systems.

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

## Records and inspection

The Inspector's job row exposes authoritative `job_execution_id`; each window row
exposes `invocation_id`, a detached `attempt_ids` list, and `resolution`. These are
not inferred from a source key, input digest, source origin, mutable stats or engine
extension metadata. A missing identity in an older native view is null, not invented.

Recorded call tuples retain their five-field format. `stats.identity_version=1`,
`stats.job_execution_id`, and `stats.record_invocations` carry identity separately.
The sidecar aligns with call records and contains exactly `invocation_id`,
`attempt_ids`, `resolution`; resolutions are `executed`, `content_hit`,
`positional_hit`, `not_submitted`, and `squash_reuse`. The last means a fresh
invocation reused an eligible squash memo outcome, not a content-store hit.
Unknown legacy sidecar prefixes may be null. If the record limit disables a job's
call record, its invocation sidecar is discarded too; the job identity remains.
Ordinary archive replay remains outcome-based. CLI-selected reexecution is a
manual selection and retains its unverified-compatibility warning; matching
outputs do not certify determinism or current implementation identity.

## Diagnostics and testing

Result eligibility uses the same Python helper from both dispatcher implementations;
native store keys use the same versioned digest domain. Tests cover revision changes,
legacy-key isolation, warn/strict incomplete identity, performance-only settings, changes
while results are in flight, setter reversion, draining detach, and later fresh jobs after
a transition. They use fixed-delay scripted engines and virtual time rather than scheduling
sleeps.
