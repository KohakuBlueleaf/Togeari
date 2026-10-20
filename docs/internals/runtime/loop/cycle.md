# Dispatcher cycle

`Dispatcher` (`src/togeari/core/runtime/dispatcher.py`) is the only thread that writes job
state. It is assembled from stage mixins:

| Mixin | Module | Holds |
|---|---|---|
| `IssueStage` | `runtime/backend/issue.py` | readiness, gates, issue, execute, stores |
| `WritebackStage` | `runtime/backend/writeback.py` | wake, fill, phis, lane hooks, deliver, complete |
| `FaultStage` | `runtime/retire/faults.py` | fault, squash, take a fault |
| `RetireStage` | `runtime/retire/retire.py` | retire, snapshots, alias checks, finish |
| `JobsStage` | `runtime/pool/jobs.py` | start, end, records, sink, admission |
| `LoopStage` | `runtime/loop.py` | events, the cycle, run attach, stop, supervision |

The front end is a separate object, `Dispatcher.front = FrontEnd(self)`.

## Inbox events

`Dispatcher.inbox` is a `queue.SimpleQueue` of tuples, handled by `LoopStage.handle`:

| Event | Posted by | Handled by |
|---|---|---|
| `("done", lane, submission, results)` | `lane_done`, from engine threads | `lane.results(...)`, then `deliver(item, result, unit)` per pair; when `lane.results` raises, every item of the submission is settled with `ItemError(exc)` and the error counted (`event_error`) |
| `("submit", SourceItem, JobHandle)` | `Machine.submit` | `start_job(item, handle)`; an exception rejects the item as `MachineError` |
| `("query", name, args, Reply)` | `Machine.inspect` | `answer(queries, ...)` |
| `("command", name, args, Reply)` | `Machine.command` | `answer(commands, ...)` |
| `("call", fn)`, `("call", fn, None)` | `Machine` (unit starts, loads after another load), `lane_woke` (`lane.capacity_rose`) | `fn()`; an exception is counted (`event_error`: logged, `stats["machine_errors"]`) and the loop goes on |
| `("call", fn, owner)` | `PluginContext.post` | `fn()`; an exception is the owning plugin's error |
| `("run", buf, sink_q, RunState)` | `Machine.run` | `attach_run(buf, sink_q, run)` |
| `("wake",)` | source reader, sink writer | nothing; it ends the wait |
| `("stop", drain, Reply)` | `Machine.stop` | `begin_stop(drain)` |

`answer` calls `fn(machine, **args)`; a returned `concurrent.futures.Future` (the `load` and
`attach` commands) settles the reply when it completes; an exception becomes the reply's error.

## One cycle

`cycle()` first calls `apply_collector()`: when the `gc_threshold` knob differs from the value
the loop last asked for (and the loop is not dead), it asks the shared collector module
(`runtime/collector.py`, `ask(owner, want)`) for it. The owner is a number from
`collector.owners`, taken when `loop()` starts and never reused.

| `collector` | Does |
|---|---|
| `ask(owner, want)` | records or replaces the owner's ask and sets the latest remaining ask, its missing later values taken from the process threshold; `want=None` withdraws the ask |
| `held()` | the asks held now, by owner |
| the process threshold | read before the first ask; replaced by a threshold set from outside the module while asks are held; set again when the last ask is withdrawn |

`withdraw_collector()` withdraws the loop's ask; `loop()` calls it when it exits and `abandon()`
calls it at once, so an abandoned loop that is still stuck holds no ask. On Python 3.14 the
second value sets the incremental collector's old-generation scan share and the third is ignored.

It then runs these phases and adds each one's time to `stats["phase_s"][phase]`:

| Phase | Does |
|---|---|
| `drain` | waits for one event up to `next_timeout()`, handles every queued event, flushes held sink records |
| `fetch` | swaps out `dirty` (job ids, in the order they became dirty); `front.advance(job)` for each resident, unpaused job; an exception is `job_error(job, exc)` |
| `issue` | `issue()` ([issue](../back-end/issue-and-writeback.md)) |
| `pump` | per lane: `lane.pump(now)`, then `deliver` each pair `lane.timers(now)` returns |
| `retire` | `retire(job)` for each job of the swapped-out `dirty` set; an exception is `job_error` |
| `admit` | `admit()` ([admission](../pool/admission.md)) |

After the phases: `stats["cycles"]`, `busy_s` (the cycle plus the time spent computing
`next_timeout`), a cycle-time histogram `cycle_hist` (bucket = bit length of the cycle's
microseconds), `cycle_max_s`, and every `CYCLE_EVENT_EVERY = 100` cycles a `dispatcher.cycle`
event with the cumulative phase times.

`next_timeout()`:

- 0 when `dirty` or `ready` is non-empty;
- otherwise the time to the nearest `lane.next_deadline(now)` ([lane](../../lanes/lane.md));
- `None` (wait for an event) when no lane has a deadline.

No thread polls: engines answer through `done` and report capacity that rose without a
completion through their wake ([lane](../../lanes/lane.md#wakes)), the source reader and sink
writer post `("wake",)` when the dispatcher asked for them, and every timed lane action is a
deadline.

`after_cycle()` ends the current run (`run.done.set()`) when no job is resident, no sink record
is held and the source is let go, or, while stopping, once no job is resident; it clears
`running` when stopping is settled, and sets or clears `on_idle`.

## Wake-ups

| Method | Does |
|---|---|
| `wake(w)` | a parked or stalled strand becomes `RUNNABLE`, is marked runnable, its job dirty, `strand.resume`; a waiting inflight is re-checked |
| `fill(slot, value)` | sets the value and `READY`, wakes every waiter |
| `resolve_phi(phi, target)` | sets `phi.forward`; moves `phi.muts` to the resolved slot; wakes the phi's waiters when that slot is filled, else moves them onto it |

A job is fetched and retired only in cycles where it is in `dirty`. Writeback, faults, strand
wake-ups, a fetch that stopped on its budget, a retire that freed capacity for a capped strand,
and `resume_job` add it. `dirty` is an insertion-ordered dict, and phis are made in name order,
so the order of decisions depends only on the program, the inputs and the order of events, not
on object ids or string hashing.

## The clock

Every scheduling time the dispatcher and its lanes read comes from one clock: `clock`
(`time.monotonic` by default) on the dispatcher and on each lane. Lane deadlines, item
timeouts, retry backoff, health polls, former waits and the `rate` estimates all use it.
[The trace driver](../../verification/trace.md) sets a virtual clock in its place. Stats that
time the dispatcher itself (`busy_s`, `phase_s`, inline run times) use `time.perf_counter`.
