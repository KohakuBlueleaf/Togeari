# Requested checkpoints

A requested checkpoint actively drives one job to a coherent sequential cut while other
jobs continue. It is separate from `snapshot_every`, which opportunistically captures
naturally eligible boundaries. A checkpoint request is not ordinary job pause: pending
prefix work must still issue and retire to make progress.

## Request and resume

Request through the machine command interface using the resident job's numeric ID:

```python
reply = machine.command("checkpoint_job", id=job_id, timeout_s=30.0, step_budget=100_000)
result = reply.result(35)
```

A successful result has `status="persisted"` and a `checkpoint_id`. It names a pinned
artifact at the actual reported cut, not merely the instant the request was received.
The job resumes ordinary scheduling after the immutable cut has been captured; persistence
runs off the dispatcher and success is reported only after the store acknowledges it.

Resume explicitly in a compatible machine, including a fresh process:

```python
handle = machine.resume(checkpoint_id, original_inputs, key=original_key, entry=None)
record = handle.result(30)
```

The original key and inputs are required. `entry=None` means the entry recorded in the
artifact, not the new machine's default. A checkpoint token may also accompany a source
item through `SourceItem.checkpoint`; that optional field is appended after `auto_key`,
preserving existing positional arguments. A different job key is rejected: resume is not
an alternative-key fork. While that key is already resident, the ordinary `DuplicateKey`
check prevents another admission. After it completes, explicitly resuming the old artifact
is allowed and may repeat external effects from the saved continuation; ownership and
idempotency remain the application's responsibility.

Normal submission never searches the pinned checkpoint namespace. Missing, corrupt,
unsupported or incompatible explicit artifacts are failures, not permission to start
fresh or fall back to an unrelated automatic snapshot. Admission validates the token,
key, entry, strong input identity and execution/layout compatibility after any
`pre_admit` transformation and before node execution.

## Inspect, cancel, and delete

Progress queries return a list of detached request records. Filter by resident job ID,
checkpoint token, or neither:

```python
requests = machine.inspect("checkpoints", job=job_id).result(5)
requests = machine.inspect("checkpoints", checkpoint_id=checkpoint_id).result(5)
all_requests = machine.inspect("checkpoints").result(5)
cancelled = machine.command("cancel_checkpoint", checkpoint_id=checkpoint_id).result(5)
deleted = machine.command("delete_checkpoint", checkpoint_id=checkpoint_id).result(35)
```

The token is allocated when the request begins, so it is available through the progress
query before persistence finishes. Cancellation returns a boolean indicating whether an
unfinished request was cancelled; it never cancels the job. Deletion returns a status
record: `deleted`, `delete_failed`, `busy`, `failed`, `timeout`, or
`persistence_unknown`. A local write with an unresolved outcome makes deletion `busy`,
preventing a late write from silently recreating an artifact just deleted locally.

Request states include `requested`, `draining`, and `persisting`. Final outcomes include
`persisted`, `persist_failed`, `failed`, `timeout`, `budget_exhausted`, `cancelled`,
`fault_interrupted`, `needs_resume`, `weak_inputs`, `incompatible`, `busy`, `terminal`,
`not_found`, and `persistence_unknown`. Details such as `reason`, `error`, and `cut` explain
the outcome. After an unknown write outcome, a late acknowledgement adds
`persistence_status="persisted"` or `"persist_failed"` to the query record without changing
the result already returned to the original waiter. Query history is local to this machine;
the pinned artifact, not this history, is what another process resumes. History targets
1,024 request records, evicting older completed records first; unfinished requests and
writes awaiting acknowledgement are never evicted. Removing history never deletes an artifact.

## What the cut includes

The runtime pins a logical frontier containing already-issued or settled work, plus
active collection completion where required. It disables new run-ahead for the requested
job and prevents unrelated younger issue, but allows the earlier dependency/control
prefix to finish. Already-issued outcomes are not replayed merely to checkpoint.

At a representable boundary, the runtime materializes the sequential continuation,
including carried state and ordinary iteration/exception context. Only never-issued
suffix work may be discarded and refetched. Values are read at the cut's logical position,
not from an arbitrary younger mutation version. The checkpoint retains the logical job
execution ID, recorded outcome/identity prefix and compatible continuation addresses.

The original program's fault semantics win. If an earlier failure invalidates already
executed younger work during a request, the initial implementation reports
`fault_interrupted` rather than pretending those speculative outcomes form a valid prefix.
Normal failure/handler recovery then proceeds. A local handler whose squash removes only
never-issued work can still permit checkpoint progress.

A collection's private builder is not a serializable sequential continuation yet. A
request can finish a finite progressing active collection and cut afterward. Ordinary
`each` and `@try` regions may be captured internally when the continuation is representable.
The runtime never drops unstarted iterations or replays already-visible ordered effects
to manufacture a boundary.

## Progress, budgets and paused jobs

Defaults are `timeout_s=30.0` and `step_budget=100_000`; both must be nonnegative,
and the timeout must be finite. The cut has a scheduling-clock deadline. An independent
real-monotonic deadline bounds the request's wait for storage as well, so virtual-clock
advancement does not replace I/O timeout handling. Checks run after
bounded fetch/control progress as well as retirement, so a control-only loop need not
retire a host call to respond. Waiting for an engine does not busy-spin. Other jobs retain
their normal scheduling opportunities.

Successful capture requires responsive engines, finite logical progress to a representable
cut, serializable values and successful storage. Responsive engines alone do not make an
infinite body or unreachable reconvergence finite. Timeout or budget exhaustion is an
explicit unsuccessful request, not a completed checkpoint. The actual cut may be later
than the requested instant; its frontier/progress is reported.

A paused job already at a valid capture boundary can be persisted as-is. A paused job that
needs execution to drain reports `needs_resume`; requesting a checkpoint does not silently
unpause it. Cancelling a checkpoint request releases its gates but does not cancel the job.
A missing or terminal job reports that state, never a fabricated successful checkpoint.

## Pinned artifact and asynchronous storage

Each request receives a fresh opaque token. Artifacts use a distinct locator domain:
`digest(("togeari.checkpoint", 1, checkpoint_id))`. They retain the original job key, entry,
program/input identity, actual cut metadata and validated snapshot-v3 bytes. They are not
overwritten by later automatic snapshots and are not deleted when the original job
completes successfully. Explicit deletion is separate.

Capture validation, encoding, and artifact packing still run synchronously on the dispatcher.
Large captured values or committed event histories can delay independent jobs, even when
the delivery backlog has drained. Off-thread storage does not remove that pause; see the
[reproducible cost measurements](../performance/event-history.md).

Only immutable artifact bytes are handed to a bounded I/O worker. Live jobs, slots and
mutable scheduler structures never cross into the store worker. Completion returns through
the dispatcher inbox with an operation-generation guard; stale callbacks cannot finish a
new request that happens to reuse an object ID.

The store's successful `put` is the persistence acknowledgment. This promises the store's
own durability contract: `MemoryStore` is not crash-durable storage. Capture or write
failure reports failure and does not increment successful requested-persistence counts.

Once a write has entered a possibly blocking backend, timeout/cancellation may report
`persistence_unknown` with the token. The write may still succeed later; querying the
request can show a late acknowledgment. A waiter timeout from `reply.result(timeout)`
is not itself a cancellation request. The runtime does not claim that a timed-out write
is absent, and does not silently erase late writes.

Store reads for explicit resume also run off-dispatcher, with a 30-second real-monotonic
read deadline. The worker pool uses two daemon threads and a bounded queue of 64 operations;
a full queue fails the operation rather than creating more threads. Pending resume admissions
keep a source run from being reported complete prematurely. Machine shutdown settles or cancels
pending requests/admissions without indefinitely joining a hung store worker.

## Identity and limits

Compatible resume retains logical job/invocation identity; new actual submissions receive
new attempt IDs. Restoring the same token concurrently in independent processes does not
provide leases, exclusive ownership, or exactly-once external effects. Applications must
supply continuation ownership or external idempotency where needed.

Checkpoint storage uses trusted pickle transport inside the snapshot envelope. Validation
protects structural/compatibility invariants, not against executing an attacker-controlled
pickle. Give the store the same trusted-writer boundary as ordinary snapshots.
