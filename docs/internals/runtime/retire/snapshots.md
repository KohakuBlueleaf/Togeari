# Snapshots

With `snapshot_every=N` and a store, a job may persist an eligible retirement
boundary. A later matching job validates execution compatibility before restoring.
This is opportunistic capture, not yet a requested checkpoint progress guarantee.

Code: `core/runtime/retire/snapshot.py` and shared `codec.py`, called from Python
retirement/admission and the native snapshot/retirement/admission implementation.
See [execution identity](../../../guides/execution-identity.md) for the public contract.

## Locator, envelope and lifetime

The v2 locator is
`digest(("togeari.snapshot", 2, program_hash, job_key, inputs_digest))`.
Weak job input digests disable snapshot lookup/capture. Execution compatibility is
validated inside the candidate envelope rather than hiding a mismatch as a new key.

Envelope fields:

| Field | Meaning |
|---|---|
| `schema` | `togeari.snapshot` |
| `version` | 2 |
| `addressing` | `region-relative-v1` |
| `execution` | declared execution manifest/completeness |
| `state` | continuation, values, frames, stats, steps and conformance record |

The old locator `digest(("snapshot", program_hash, job_key, inputs_digest))` is probed
only when v2 is absent, to report migration-needed v1 data. V1 is never applied or
relocated by guessing its former code-memory base. Incompatible v2 never falls back.

A successful write advances `snap_at` and `stats.snapshots`; store failure does not.
Successful job completion deletes a snapshot it created/resumed (or writes `None`
if deletion is unavailable). Failure retains it. Invalid/rejected candidates stay
stored for diagnosis. Positional auto-keys still warn about cross-run stability.

## Capture

After retirement, `capture(job, code, manifest)` returns bytes or `None` off a boundary:

1. No active collectors, empty window, exactly one live non-spawner strand.
2. A parked branch rewinds its continuation to the branch before address encoding.
3. Every live name resolves to a ready value.
4. Remaining joined frames have representable sequential continuations.
5. Execution identity/history permits persistence under the selected mode.

The raw state fields are `pc`, `prefix`, `counter`, `frames`, `values`, `stats`,
`steps`, and `record`. Frames carry `reconv`, `saved`, `branch`, `kind`, `each`,
`items`, `i`, `base`, and `joined`. The codec converts **only** `pc` and frame
`reconv`/`branch`/`each` to offsets from the entry region start. Logical order keys,
iteration bases, saved prefixes and counters are not instruction addresses.

Pickle protocol 5 preserves the entire user-value alias graph. Capture validation
or serialization failure is reported explicitly and disables further opportunistic
attempts for that job as appropriate; it does not claim a completed persistence.
The internal low-level capture helper requires the actual `CodeMemory` for v2.

## Candidate selection and preflight

`load_candidate(store, new_key, old_key, on_error=None)` returns an explicit action:

- `new`: both candidate locators are genuinely absent (`MISS`/`None`).
- `candidate`: v2 bytes exist and need preflight.
- `reject`: read failure or legacy candidate, with a structured decision.

`prepare_restore(blob, entry, code, current_manifest, mode)` is shared between
Python/native consumers. It validates envelope/version/schema, execution/layout
compatibility, exact structural types, signed-i64 structural integers, address
ranges and frame-to-opcode relationships before any live-state mutation. User values
are not restricted to structural integer bounds. Invalid frames do not silently
become branch frames.

The returned state has validated absolute addresses in the **current** region.
Joined iteration reconvergence is recomputed after relocation. Logical keys and
five-field record tuples are unchanged. User values are not copied separately.

A present incompatible, corrupt, unsupported, v1 or unreadable candidate rejects only
that job as `SnapshotIncompatible`, with `stats.snapshot_restore` issues. It does not
restart potentially effectful work, enter the job pool, start engine calls, or notify
`on_job_start`. Normal cache read errors may remain misses; snapshot reads deliberately
have the stronger continuation-safety rule.

## Atomic application

Python builds temporary frames, names, strand, stats and recording state before
ending the fresh strand. Native extracts all fields into a detached DTO and stages
stats/record conversions before interning names or replacing strands. No unchecked
blob fields are read after commit begins (allocation failure is not a transactional
storage guarantee).

Restored values become ready slots without independent deep copies. Frames have no
live scheduler joins. Recording continues the saved prefix when available; otherwise
`record_skipped` explains the absence. `resumed=True` and `snapshot_restore` retain
the compatibility status. Machine-local `program_region`, admission execution and
epoch come from the current job, not the old process's stats.

The low-level `restore(...)->bool` wrapper remains for internal callers; `False`
means denied, not permission to restart. Admission uses the structured candidate
and preflight result so absence cannot be confused with incompatibility.

**Trust:** validation protects runtime consistency, not against code execution by
malicious pickle. Snapshot stores must remain writable only by trusted writers.
