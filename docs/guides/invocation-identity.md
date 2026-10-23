# Logical jobs, invocations, attempts, and reuse

A source key, an input digest, and a source location answer different questions from an
execution identity. Two calls can have equal inputs while intentionally requesting two
independent samples. Neither a cache hit nor a generated ID makes external effects exactly
once.

## Identity layers

| Field | Meaning | Representation |
|---|---|---|
| `SourceItem.key` | Application/source lookup key; unique among currently resident jobs | User string |
| `job_execution_id` | One logical run, fresh on new admission and preserved by compatible continuation | Random UUID4, 32 lowercase hexadecimal characters |
| `invocation_id` | One dynamic call in that run | Versioned SHA-256, 64 lowercase hexadecimal characters |
| `attempt_id` | One actual inline execution or per-item engine submission | Fresh UUID4, 32 lowercase hexadecimal characters |

An invocation hashes the format version, job execution ID, program hash, execution hash,
region-relative callsite, and logical control/iteration order tuple. Inputs, source paths,
source origins, graph-node IDs, absolute code addresses, and attempt IDs are excluded.
The stability domain is the same compatible lowered program/layout and execution contract;
arbitrary program edits are not promised to preserve invocation IDs.

The runtime pins the program and execution identity in authoritative job state at admission.
Invocation IDs use that pinned state, not mutable copies in diagnostic `stats`; editing a
reported manifest or source metadata does not redefine a machine-owned invocation. This
identity rule does not make all mutable plugin diagnostics an untrusted-input boundary.

Resubmitting the same source key and inputs without restoring a compatible continuation is
another logical run, not automatic recovery of the old run. There is no caller-supplied job
execution ID on `SourceItem`. Scheduling counters and batch submission IDs remain separate
from the durable per-call identities.

## Five call outcomes and execution paths

| Path | Logical invocation | New actual attempt? |
|---|---|---|
| Fresh inline or engine execution | Identifies this dynamic call | Yes |
| Retry after an eligible unit failure | Same invocation | Yes, for each actual resubmission |
| Eligible content-cache hit | Current call's invocation, not the producing call's ID | No |
| Positional recovery | Exact compatible logical invocation | No, when its stored outcome supplies the call |
| Deterministic squash memo hit | Refetched call's own invocation; not proof it is the earlier invocation | No |

Machine-bound work items expose read-only job/invocation/attempt identity. Raw ownerless
work items may have `None` for their logical job and invocation IDs; actual lane submission
can still allocate an attempt ID. Each item in a batch gets its own attempt ID, not the
batch submission ID.

Attempt allocation happens after the complete `pre_submit` transformation chain and deadline
checks, immediately before the engine's submit call. The first `pre_submit` sees no attempt;
on retry it sees the latest previous attempt until a new submission begins. A blocked call,
first-attempt queue expiry, or cache/memo hit creates no attempt. A submit call that raises
still counts as an attempt. If a previously submitted call expires during retry backoff, it
retains its earlier attempt history but gains no new attempt. Recovery after process restart
likewise must not reuse old attempt identities.

The owning inflight is authoritative for machine-bound identity. Replacement work items are
reconciled after the **whole** plugin transformation chain, not between every plugin. Later
plugins in the same chain must not assume every intermediate replacement was already
reconciled.

Machine plugin replacements must be **exact `WorkItem` instances**, not subclasses: subclasses
can override identity properties or transport methods. An unsupported replacement is rejected
as `Blocked` before a new attempt is allocated. To migrate a plugin that returned a subclass,
return a base `WorkItem` and carry custom metadata in its `ext` dictionary instead; identity
fields remain runtime-owned. This is a supported-contract guard, not a security boundary.
Standalone subclass-based engine transport remains outside the machine-bound identity
guarantee.

### Recorded resolutions

The adjacent `stats["record_invocations"]` sidecar aligns with the unchanged five-field call
record tuples. Each entry contains `invocation_id`, `attempt_ids`, and one of five resolutions:

- `executed`: actual inline/engine work, including any submitted retries;
- `content_hit`: another eligible deterministic call supplied the result;
- `positional_hit`: exact compatible invocation recovery supplied the result;
- `squash_reuse`: deterministic memoization supplied the refetched call's result;
- `not_submitted`: the call failed or was blocked before an actual attempt.

Retries appear as multiple attempt IDs under `executed`, not as a sixth resolution. Cache and
memo resolutions have no attempts. This sidecar explains how a call resolved; it does not
turn a refetched deterministic call into the old invocation or promise external deduplication.

## Deterministic reuse declarations

`Use` and `Inline` append `deterministic: bool | None = None`. This is separate from both
revision fields: knowing which implementation runs does not prove that it is deterministic.

| Declaration | Resolved meaning |
|---|---|
| `deterministic=True` | Explicit determinism assertion, subject to effect and persistence safeguards |
| `deterministic=False` | Disable cross-invocation content and squash reuse, including builtin defaults |
| `None`, custom host implementation, no content store | Not declared deterministic |
| `None`, recognized exact standard/internal/record operation | Builtin deterministic default under its declared value semantics |
| `None`, explicit `store="content"` | Legacy determinism assertion, with a construction warning; migrate to explicit `True` |

`store="content"` with explicit `deterministic=False` is a construction error, not a silent
cache bypass. A revision alone never enables deterministic reuse. A library name or node
spelling cannot inherit builtin eligibility: replaced implementations and record-operation
subclasses require an explicit declaration. A matching `deterministic=False` also disables
reuse for compiler-generated collection helpers, which still execute inline.

Standard operations such as addition and string conversion assume the declared value
semantics. Applications supplying custom stochastic or effectful Python overloads must bind
those operations with `deterministic=False`; automatic builtin recognition is not an audit
of arbitrary host-object behavior.

Ordered and mutating calls never use result stores or squash memoization, even with
`deterministic=True`. Both content and positional store declarations on a mutating call are
ignored with an explicit construction warning. A cached return value alone does not restore
mutated input ports, even when the invocation ID matches; mutation-outcome envelopes are not
supported in this increment.

Migrate direct mutating-call stores by removing `store=` and letting the call execute. A
nonmutating producer may still use positional recovery when a downstream call mutates its
output: the existing copied-result safeguards for that distinct case remain supported.
Do not treat this as general alias or mutation-state restoration.

The execution manifest includes resolved determinism and eligibility. Explanatory
`reuse_source` values (`none`, `binding`, `builtin`, `legacy_content`) do not enter identity:
a legacy content assertion and an explicit `True` with the same effective contract match.
Memo capacity settings do not authorize reuse of undeclared sampling operations.

## Continuation and migration

Snapshot version 3 carries the logical job execution ID. Compatible restore preserves it
before future calls are identified. Older snapshots without that identity require explicit
migration; the runtime must not invent a new random identity and certify it as the old run.
Versions 1 and 2 are not silently resumed or treated as permission to restart effectful work.

Restoring the same snapshot concurrently in two machines preserves the same logical job ID
and corresponding invocation IDs in both. Those identities do **not** coordinate the machines
or exclude duplicate physical execution and effects. The caller must enforce continuation
ownership or use an appropriate external idempotency contract. Source-key uniqueness among
resident jobs is local to one machine, not a global duplicate-execution guard.

Positional-v3 keys identify the logical invocation. Older position-based namespaces are not
silently reused. Content caching remains a distinct optimization across eligible deterministic
calls and still requires compatible execution identity and usable input digests.

See [execution identity](execution-identity.md) for warned versus strict persistence and
[result reuse](result-reuse.md) for store behavior. Readable legacy conformance records do
not retroactively gain invocation identity; the five-field call tuples remain unchanged,
with identity metadata recorded alongside them.

Engines may map an invocation ID to a seed or service idempotency key. That requires an
explicit engine/service contract: generated identifiers alone provide neither deterministic
external outcomes nor exactly-once delivery or effects.
