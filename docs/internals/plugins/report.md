# Run-report policy views

`plugins/report/spec.py` writes the explanatory construction snapshot in
`spec.json`. Existing program, binding, lane, unit, policy and knob fields remain
available. The following additions expose resolved execution policy separately
from scheduling preferences. The descriptive `calls`/lane fields are not identity
proof; a separate versioned `execution` manifest now reports declared identity
and explicitly records missing attestations.

## Execution manifest and source identity

Each `programs[region].execution` in a freshly generated spec contains the full
current effective manifest from `current_execution(region, code, machine)`:
`schema`, `version`, `identity`, `hash`, `completeness`, and the process-local
observation `epoch`. `completeness` has `complete`, `unknown` path/reason entries,
and `transitions`. An unknown identity is **not certified**, even when its hash
matches another incomplete manifest. Explicit host/binding revisions are
attestations, not automatic code fingerprints or determinism proofs.

The existing `semantic_hash`/`full_hash` still identify the program; they are not
replaced with the execution hash. Source origins remain separate from identity.
Lane/unit placement names and performance configuration remain in the report's
descriptive fields rather than being promoted into portable execution identity.
Changing an observable timeout can change `execution.hash` without changing the
program hash. Generating this view observes current effective policy/membership;
it does not change either. `spec.json` is a snapshot at report creation, while
Inspector queries build a fresh current view when requested.

## Archived execution metadata and replay

Schema-2 program artifacts optionally include `execution` and
`execution_at: "region_binding"`. Recorder writes the **initial** `Region.execution`,
not a manifest recomputed when the first recorded job happens to finish. Exact
run/region artifacts prevent a later run with the same semantic program hash but
a different implementation revision from overwriting earlier execution metadata.
The legacy global program index remains a latest-version convenience index.

A recorded job's `stats.execution`, `stats.execution_epoch`, and any transition
history supplied by the runtime are retained independently, as are existing
policy-failure sidecars. The program artifact must not be mistaken for the
per-job execution context after a policy change. No invocation or attempt IDs
are introduced by this reporting layer.

Ordinary conformance replay still consumes recorded outcomes and reads legacy
artifacts without an execution manifest. It does not require a newly imported
host implementation or engine to have the recorded identity. Selected node
reexecution emits one warning per run/program/region that implementation/binding
compatibility is unverified: the CLI does not receive a current revision
attestation or bound-machine manifest. Matching outcomes are not identity
certification, and an incomplete archived engine identity remains unverified.
Persistence compatibility enforcement is a separate runtime concern.

## Effective call rows

Each `programs[region].calls` list contains one row per bound call instruction:

| Field | Meaning |
|---|---|
| `addr`, `node` | decoded instruction address and node spelling |
| `origin` | structured source provenance, including imported definition and call ancestry |
| `pattern` | binding pattern actually selected for this instruction |
| `inline` | whether execution is on the dispatcher |
| `lane` | resolved lane name, or `null` for inline calls |
| `execution_policy.source` | `"binding"`, `"legacy_hint"`, or `"none"` |
| `execution_policy.timeout_s` | effective per-call timeout, or `null` for no call deadline |

These rows use the already-resolved `Bound` values, not a fresh interpretation of
hints or the binding configuration. Inline calls have no call timeout; ignored
legacy inline timeout hints remain visible in construction warnings. An explicit
empty `ExecutionPolicy()` has source `"binding"` and timeout `null`, and overrides
a legacy timeout hint just as an explicit numeric timeout does. Zero is retained
as zero, not treated as an absent timeout.

`bindings[pattern].policy` separately describes the supplied configuration:
`null` means no explicit policy, while `{"timeout_s": null}` means an explicit
empty policy. Binding configuration is fixed for a machine. Mutating `m.bind` or
a `Use` object is **not a supported rebind** of loaded regions; such mutation may
make the configuration description differ from the effective call rows. The rows
continue to report what is bound. Documented lane-policy commands are distinct
from program rebinding.

## Lane policy groups

Lane rows retain their existing flat fields and add:

- `observable_policy`: `timeout_s`, `retry` (type and primitive attributes), and
  `lane_dead_s`. Timeouts, retries, and availability failures affect observable
  execution outcomes.
- `advisory`: `former`, `select`, `forming`, and `max_inflight`. These describe
  scheduling preferences and performance configuration; they do not promise
  stochastic timing invariance or make cancellation modes merely advisory.

Per-call and lane timeouts are not collapsed into one reported number: their
clock origins differ, and runtime enforcement combines their deadlines. This
snapshot does not predict a job's future deadline, retry outcome, or cancellation.

The Inspector's [`programs` query](inspector.md#effective-call-policy) exposes the
same detached effective call rows. Reading these views performs no execution and
changes no policy.
