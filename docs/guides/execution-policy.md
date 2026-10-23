# Execution policy and deadlines

Execution policy can change which result or exception a program observes. It is distinct
from scheduling hints such as priority or batch wait preferences. A call deadline belongs
in the machine's binding configuration:

```python
from kohakunode import NodeLibrary

from togeari import ExecutionPolicy, Machine, Use
from togeari.engines.python import PythonCallEngine

lib = NodeLibrary("lookup")


@lib.node()
def lookup(x: int) -> int:
    return x + 1


with Machine(
    program="@def (x: int)main(y):\n    (x)lookup(y)\n",
    entry="main",
    libraries=[lib],
    bind={
        "lookup": Use(
            PythonCallEngine(workers=1, mode="thread"),
            policy=ExecutionPolicy(timeout_s=1.0),
        )
    },
) as machine:
    record = machine.submit({"x": 3}).result(5)
    assert record.ok and record.outputs == {"y": 4}
```

`ExecutionPolicy` is immutable. `timeout_s` is a finite nonnegative duration or `None`;
zero means immediate expiry. Policy is resolved when the call is bound, before jobs run.
The effective bound policy is separate from the configured binding mapping. Mutating
`machine.bind` or a `Use` object is not a supported way to rebind already loaded code.
Construct a machine with the intended configuration; inspection shows the resolved policy
and its source.

## What the clocks cover

| Limit | Starts | Covers |
|---|---|---|
| `Use(..., policy=ExecutionPolicy(timeout_s=T))` | initial work-item preparation for enqueue, after effect readiness and a store miss | queue wait, preparation/submission, all attempts and retry backoff |
| `Lane(..., timeout_s=T)` | each attempt's submission time | that submitted attempt |
| manually constructed `WorkItem.deadline` | absolute monotonic timestamp provided by caller | the call across queueing, attempts and backoff |

The earliest active deadline wins. Exact ties are attributed to the call-wide policy.
Retry never restarts the original call budget; a new attempt gets its own lane timeout,
still bounded by the unchanged call deadline. `Lane.retry` remains the existing observable
retry/failover configuration, not an advisory hint; this change does not introduce per-call
retry configuration or alter which errors are retryable.

The lane refreshes its scheduling clock after `pre_submit` and again after resource
re-estimation, compatibility-key evaluation and repacking, before allocating submission or
attempt state. Items whose call caps have expired are held for ordinary timeout settlement;
live batch members keep their order and resource charges. The per-attempt timeout starts
at this final submission timestamp. This does not preempt callbacks while they run.

A call cap of zero expires before engine submission. A lane attempt timeout of zero starts
at submission and can expire that attempt immediately. No deadline interrupts Python code
already executing inline: `Inline()` has no execution timeout. Its latency warning remains
an advisory diagnostic, not cancellation.

Deadlines use the dispatcher's monotonic scheduling clock. **Completion is judged when the
dispatcher observes the result**, not an engine-supplied completion timestamp. A result
observed at or after the effective deadline is expired even if the timer sweep has not run
yet. This closes the old result-before-timer race. A late success cannot resurrect an item
already settled by timeout. Cancellation is best effort: side effects or remote work may
continue after the machine has taken the timeout. Declare ordered behavior and retry safety
accurately; a timeout is not a transactional rollback.

Store hits precede work-item creation, so a hit bypasses the call deadline entirely. This is
intentional precedence, not a zero-budget cache lookup timeout. Effect-dependency waiting
also occurs before the call budget begins. The policy is not a wall-clock limit on the
whole job, compilation or input admission.

## Legacy `@hint timeout_s` migration

Existing timeout hints are supported as an explicit compatibility conversion at binding:

| Binding | Legacy hint | Effective call policy |
|---|---|---|
| `Use(..., policy=None)` | absent | no call deadline |
| `Use(..., policy=None)` | present | normalized legacy deadline, with migration warning |
| explicit `ExecutionPolicy(timeout_s=T)` | any | explicit policy wins, with override warning |
| explicit `ExecutionPolicy()` | any | no call deadline, even if the hint specified one |
| `Inline()` | present | ignored, with warning |

The empty explicit policy disables only the call cap; it does not disable a lane's
per-attempt timeout. Legacy values retain the prior float-conversion compatibility where
valid, including numeric strings and booleans. A legacy value of `None` disables its cap.
Invalid legacy values now fail construction rather than failing a job later. Explicit
policy overrides do not validate an ignored malformed hint. Explicit-policy validation
follows `ExecutionPolicy`'s stricter constructor contract.

Two migration changes are observable:

1. Invalid effective legacy timeout configuration is rejected during binding.
2. Results observed exactly at or after a deadline expire consistently, rather than
   occasionally winning because result delivery happened before the next timer sweep.

Remove timeout hints once bindings carry the intended policy. Other hints remain hints;
the runtime no longer reads `Instr.hint["timeout_s"]` while preparing a call.

## Failure attribution without changing KIR errors

Enforced expiry still raises the ordinary catchable `ItemTimeout`. The error value seen by
KIR `@except` keeps its existing `kind`, `node` and `message` fields. Recorded calls keep
their existing five-field tuple. No policy envelope is inserted into either contract.

Instead, enforcement attaches a cause to the work item:

```json
{
  "reason": "deadline",
  "scope": "call",
  "timeout_s": 1.0,
  "source": "binding"
}
```

`scope` is `call` or `attempt`. `source` is `binding`, `legacy_hint`, `lane` or `work_item`.
For a manually supplied absolute deadline, the configured duration is unknown and
`timeout_s` is null. Attribution uses the original configured duration, not the remaining
budget at the last retry.

At precise fault retirement, `JobRecord.stats["policy_failures"]` receives a sparse entry
with that cause plus `node`, `addr`, the existing runtime order `key`, `origin`, and the
zero-based `record_index` (null when the job was not recorded). Only faults actually taken
are added; younger squashed work does not become a committed policy failure. There are no
new invocation IDs in this representation.

`lane.result`, `fault.taken` and `on_fault` carry a `policy_failure` field when enforcement
caused the failure. Recorder archives preserve the sidecar in the existing stats mapping;
run reports retain stats/events through their existing paths. A host function that raises
an exception named `ItemTimeout` has **no policy attribution** unless the dispatcher itself
also enforces an expired deadline. Inspect the structured cause, not just the exception name.

An expired item's `UnitError` still marks the unit down: enforcing the item budget must not
hide unit-health information. In a batch, expired items fail independently while live
items retain their normal success or retry handling. A success discarded by enforcement is
not counted as a successful service completion.

## Validation model

Tests drive ordinary Python and native lanes using a virtual clock and fixed-delay scripted
engines. They exercise queue and inflight expiry, exact-deadline result delivery before the
timer sweep, retry/backoff without resetting the cap, minimum/tie arbitration, late-result
suppression, mixed batches, host-error distinction, store-hit bypass and archive replay.
No wall-clock sleeps are needed to establish deadline ordering.
