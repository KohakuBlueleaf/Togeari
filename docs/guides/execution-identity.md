# Execution identity and persistence compatibility

A program hash is not a fingerprint of its host implementations. Togeari keeps
source provenance, linked program identity, resolved signature contracts and declared
execution identity separate. Execution manifests describe what the machine was
configured to execute; they do not prove determinism or exactly-once external effects.

## Declared revisions

`Use` and `Inline` accept two independent optional strings:

- `implementation_revision`: revision of the host/node implementation only.
- `binding_revision`: an explicit author attestation covering the whole resolved
  implementation bundle: host behavior, adapter, eligible engines/services/models,
  and semantic configuration.

```python
from togeari import ExecutionPolicy, Use

binding = Use(
    lane,
    implementation_revision="transform-function@3",
    binding_revision="transform-service-bundle@7",
    policy=ExecutionPolicy(timeout_s=2.0),
)
```

These are opaque nonempty strings, not inferred function names, source paths, or
pickled objects. If both are supplied, both contribute to identity. Update the
appropriate revision when covered behavior changes. A bundle declaration permits
strict persistence with existing engine contracts that do not expose revision hooks;
it is a declaration of coverage, not independent verification of the engine.

Recognized standard/internal operations, generated record operations, and supported
builtin adapters have maintained implementation revisions. Custom subclasses or
same-named replacements are not automatically recognized. Unknown components and
nonportable signature defaults remain explicit completeness gaps. Ordinary execution
is still possible without declaring revisions.

The configuration bound into loaded instructions is fixed for a machine. Changing
`machine.bind`, a `Use`/`Inline` declaration, or construction defaults is not a supported
rebind. Subsequent loads validate the original declaration fingerprint instead of
silently reusing stale bindings. Declare bindings up front, including those for programs
that will be loaded later, or construct another machine.

## Manifest layers

A version-1 `togeari.execution` manifest contains:

- **Program:** linked semantic hash, dependency identity, entry and selected passes,
  and resolved signature contract identity.
- **Layout:** versioned decoder/instruction layout, relative entry and control-flow
  positions, and instruction count. This permits validating continuation addresses.
- **Calls:** region-relative positions, signatures/effects, declared implementation
  and bundle revisions, component labels, and effective observable policies.
- **Runtime policy:** the supported operational limits included by this version.
- **Completeness:** explicit unknown paths/reasons and unsupported mixed histories.

The manifest hash covers its versioned identity, not source origins, checkout paths,
human lane/unit names, diagnostic policy provenance, or a machine-local epoch. Equal
binding and migrated-legacy policies have equal effective policy identity. Performance
preferences such as batching, worker count and replica count are not implementation
revisions. A hash over unknown markers is not a certificate of compatible execution.

Portable constants use a typed canonical encoding. Integer size and float bit patterns
are retained; opaque host values produce an unknown-identity reason rather than being
fingerprinted by `repr` or arbitrary pickle bytes. This policy does not restrict the
ordinary node-value domain to JSON.

## Observable configuration changes

Call deadlines, lane attempt deadlines, retry limits/backoff, and availability limits
contribute to execution identity. Supported lane policy/unit changes are observed at
their mutation boundaries and before persistence checks. Machine-local epochs detect
change-and-revert histories even if the final effective policy equals its initial value.

A job pins its admission manifest and local epoch in stats. A job that spans a relevant
configuration transition has unsupported mixed history for persistence in this initial
version. New jobs observe the new effective configuration. Epochs are diagnostic local
ordering, never portable identity and never compared across restarted machines.

Draining/detached units are no longer eligible; temporarily down units remain members
of the recoverable implementation set. A changed eligible membership invalidates the
original bundle attestation's coverage. An additional replica is not silently proof that
its implementation matches. Construct a machine with a fresh correct attestation when
persisted compatibility must be certified. Direct list mutation followed by restoration
without the supported attach/detach API is not a supported configuration transaction.

## Warn and strict modes

`Machine(persistence_compatibility="warn")` is the migration default:

- Compatible complete manifests allow persistence reuse/resume.
- Incomplete but otherwise matching new manifests may be used with visible warnings
  and an `unverified` status; this does not claim safe implementation fingerprinting.
- Known identity mismatches and unsupported mixed histories are rejected.

`persistence_compatibility="strict"` also denies incomplete identity. Normal execution
remains available; ineligible result-cache access is a diagnosed miss. A present
incompatible snapshot is a rejected continuation, not permission to restart silently.
The mode is construction configuration, not a mutable scheduling knob.

Warnings are deduplicated by execution identity and reason. Per-job stats retain the
actual compatibility decision. Fresh machine-spec/Inspector queries expose current
`programs[].execution`; the on-disk `spec.json` remains the snapshot written when
RunReport loaded, not a continuously rewritten view. These are distinct from the
initial region-binding manifest archived with program metadata. Per-job stats retain
admission identity and transition/persistence information.

## Snapshot version 3

New snapshots require an authoritative `job_execution_id` in the version-3
envelope and state, alongside the execution manifest and
`addressing: "region-relative-v1"`. The continuation PC and frame `reconv`, `branch`
and `each` addresses are offsets from the entry's region. Logical order keys, counters,
iteration bases and saved prefixes are not instruction addresses and are not relocated.

Restore validates the envelope, execution/layout compatibility, exact structural field
types/ranges, frame kinds and code relationships **before** replacing strands, interning
native names or changing job stats. Python and Rust use the same preflight contract.
Values preserve their unpickled alias graph; they are not independently deep-copied.
Structural integers share the native signed-64-bit domain; user values retain their
ordinary types and integer range.

The new snapshot locator has a distinct versioned domain. If it is absent, the old
locators are probed only to identify migration-needed data. Version-1 absolute-address
and version-2 snapshots without the required logical-run identity are rejected as migration
needed, not automatically applied. A future explicit offline migration needs verified
old layout/base and invocation identity; inventing a new run ID cannot recover the old run.

A missing candidate starts a fresh job. A present corrupt, unsupported, incompatible,
or legacy candidate—or a store read failure—rejects only that job with
`SnapshotIncompatible`, an explanation in `stats.snapshot_restore`, and no engine work.
It is not silently treated as a miss that could repeat external effects. The candidate
is retained; no fallback from an incompatible v3 candidate to old data occurs.

Capture still requires the existing safe boundary; active collectors are not serialized
by this increment. Reliable requested cuts are a separate feature. Snapshot counters
advance only after successful persistence acknowledgment. Pickle remains trusted
transport: structural validation does not make unpickling attacker-controlled data safe.

## Logical invocation and result reuse

Every fresh job receives a `job_execution_id` (32 lowercase hex characters), independent
of source key and inputs. Compatible snapshot resume preserves it. Each dynamic call has
an `invocation_id` (64 lowercase hex characters) derived from that logical run, program
and execution identity, relative callsite and logical control/iteration position. Actual
inline execution or engine submission gets a new 32-hex `attempt_id`; retries retain the
invocation and add attempts. Content/positional hits and deterministic squash reuse do not
invent execution attempts. These tokens are not exactly-once guarantees for external effects.

Content keys remain in the version-2 execution-aware domain. Positional keys use version
3 with execution hash and invocation ID; old positional keys are not silently reused.
A completed fresh same-key/equal-input rerun is a new logical run and does not recover
old positional results. A compatible resumed invocation can recover its committed result.
Compatibility is checked at lookup and again before writing results, so an in-flight
configuration change cannot publish under an obsolete execution namespace.

`Use(..., deterministic=True)` or `Inline(deterministic=True)` explicitly declares
eligibility for content/squash reuse, separately from revision attestations and retry
idempotency. Known builtin operations have maintained eligibility; arbitrary host calls
are not implicitly deterministic just because they lack mutation flags. Legacy explicit
`store="content"` without the flag is accepted with a migration warning as an assertion;
`deterministic=False` with content storage is an error. Ordered/mutation safeguards remain
in force. All store modes on directly mutating or ordered calls, including positional,
are ignored with a construction warning: cached return values do not reconstruct mutation
outcomes. A pure producer's pre-mutation result may still be recovered before a later
mutating consumer; no general mutation-replay envelope is added. See
[result reuse](result-reuse.md) for eligibility and migration details.

A declaration is not independent proof of deterministic sampling, environment isolation,
or engine equivalence. Logical identity and attempts do not provide transactional
side-effect deduplication at an external service.

## Recording and conformance

Legacy conformance archives remain readable. Ordinary replay checks a recorded outcome
trace against the reference, not whether today's service has the recorded implementation.
Schema-2 program artifacts optionally retain `execution` and
`execution_at="region_binding"`; job `stats.execution` and `stats.execution_epoch`
retain admission information separately. Selected-node re-execution currently warns
once per run/program/region because this CLI supplies no current revision attestation,
even when the archived manifest is complete. Matching outputs alone are not identity
certification.

Call records keep their five-field tuple shape. Identity is a separate aligned
`stats.record_invocations` sidecar under `stats.identity_version=1`, with each known
row `{invocation_id, attempt_ids, resolution}`. Resolutions distinguish `executed`,
`content_hit`, `positional_hit`, `not_submitted`, and `squash_reuse`. Null legacy-prefix
entries remain unknown. `stats.job_execution_id` records the logical run; disabling a
call record at `record_limit` drops the invocation sidecar too, not the job identity.
Inspector job/window identity fields come from authoritative runtime attributes rather
than mutable stats or engine extension dictionaries.
