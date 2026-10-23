# Committed semantic events

KIR's standard ordered `emit(topic, payload)` publishes semantic output, not a
lossy diagnostic event or a partial engine result. Engines still return one
terminal result per work item. Authentic standard aliases such as `s.emit` work;
a host implementation merely named `emit` remains an ordinary node.

```text
@def (items: list)main(count):
    (items)each(item, `body`)
    body:
        ("item", item)emit()
    (items)len(count)
```

## Explicit output configuration

An emitting machine requires both a sink and a journal:

```python
from togeari import EventOutput, Machine
from togeari.io import MemoryEventJournal, MemoryEventSink

journal = MemoryEventJournal()
sink = MemoryEventSink()
output = EventOutput(sink, journal, max_events=64, max_bytes=8 << 20)
machine = Machine(program='("status", {"ready": True})emit()\n', event_output=output)
```

`MemoryEventJournal` is explicitly nondurable. `SQLiteEventJournal(path)` provides
a durable outbox with a persistent journal identity; the application owns closing
the journal, which may outlive an individual machine. The output manager owns
sink delivery/close. `max_events` and `max_bytes` bound outstanding events;
reservations remain charged until sink delivery and journal acknowledgment finish.
An event larger than the byte limit fails before commitment rather than waiting
forever. Portable capture rejects opaque host objects, cycles, nonfinite numbers
and unsupported values; mutable payloads are snapshotted before execution hooks.

## Commitment is not delivery

At the sequential retirement boundary, the event waits for journal append
acknowledgment before the emission retires. A pending append blocks younger
ordered effects. Earlier committed events remain committed if a later node
fails, a job is cancelled, or a KIR `@try` handler runs. `@try` is not a transaction
that rolls back already committed output.

The sink runs off the dispatcher. Delivery is **at least once**: retries reuse the
same event ID. A consumer should deduplicate that ID atomically with its own
effect if duplicate effects are unacceptable. Neither a successful terminal job
record nor a committed watermark means the consumer has acknowledged every event.
Cancellation during an accepted append must resolve possible commitment; bounded
shutdown may report an unknown append outcome, not falsely claim no event existed.
Unacknowledged durable events are recovered from the journal on restart.

Failure stages remain distinct on both cores:

- Invalid or oversized payloads rejected before append are catchable node-validation failures.
- A failed commitment token (definitive append conflict or journal recovery failure) is a
  terminal `KirRuntimeError`, not catchable by KIR `@except`. The diagnostic retains the
  rejection cause and source attribution. Younger ordered effects do not execute, and
  previously committed events remain the prefix; rejection invents no committed event.
- Transient append errors retain the existing retry behavior and stable event identity.
- Sink errors after commitment retry delivery/acknowledgment without undoing the prefix.
- Ambiguous append outcomes during shutdown remain explicitly unknown, not absent.

`CommittedEvent.to_dict()` returns a detached envelope containing `schema`,
`version`, `job_execution_id`, `sequence`, `event_id`, `invocation_id`, `topic`,
`encoded_payload`, and `origin`. Sequence starts at zero per logical job, and event
identity derives from the emission invocation—not payload equality or delivery
attempt. Equal payloads from distinct calls are distinct events. Origin is
explanatory and excluded from semantic append deduplication. Payload containers
use the KIR portable tagged representation; internal `encoded_bytes()` uses a
lossless canonical transport including hexadecimal integer tags for large integers.
Use `CommittedEvent.from_bytes()` for that transport, not plain JSON-to-envelope
conversion.

## Inspecting and recording

`machine.inspect("event_output", job_execution_id=uid)` returns detached
`{enabled, configuration, status}` from the manager's cached state. Omitting the
UID requests aggregate state. Inspection never contacts the sink or journal.
Status separates append-pending work, durable pending delivery, committed/delivered
counts or a per-job watermark, and recovery/delivery errors. The run report's
`event_output` is a snapshot at report creation; it is not a continuously updated
acknowledgment log. Diagnostic bus sampling never controls semantic delivery.

Recorded jobs preserve unchanged five-field call tuples and a separate
`stats.committed_events = {"version": 1, "events": [...]}` sidecar containing the
full committed prefix, including a restored prefix. Invocation associations come
from `stats.record_invocations`; delivery retries are not duplicate semantic rows.
The ordinary archive stats encoding retains these fields.

Conformance replay regenerates reference semantic events from recorded node
outcomes without opening a consumer or journal. It checks sequence, topic,
type-sensitive portable payload, event identity, logical job and associated
invocation, plus the existing final outcome checks. `Verdict.event_status` is
`verified`, `mismatch`, `unverified`, or `not_applicable`. A legacy emitting trace
without its event sidecar is **unverified and does not pass full conformance**;
a nonemitting legacy trace is unaffected. CLI output explicitly counts event
mismatches/unverified traces. Origin changes alone do not change event semantics.

## Checkpoint boundary

Emitting continuations carry a version-1 `event_state` cursor with `next_sequence`,
`committed_watermark`, and `journal_identity`. Restore asynchronously validates the
journal frontier before any node executes. A checkpoint behind a newer committed
frontier is rejected; event-ID deduplication cannot make divergent stochastic
reexecution safe. These rules do not provide leases, distributed ownership or
exactly-once external effects.
