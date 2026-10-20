# Event bus

`Bus` in `src/togeari/core/observe/bus.py` carries every event the machine emits. The
dispatcher only counts, calls cheap listeners and appends to bounded queues; each subscriber
drains its own queue on its own thread.

## Listeners

Listeners are synchronous callbacks `fn(kind, fields)` on the dispatcher thread; they must be
cheap and must not keep the fields' objects. `emit` runs on the dispatcher thread, or while the
machine is stopped: `PluginContext.emit` from another thread while the machine runs posts the
emit to the dispatcher (`("call", fn, plugin)`), so `counts` and the listeners are never used
by two threads at once. An emit posted as the machine stops may not run.

| Method | Does |
|---|---|
| `listen(fn, kinds=None)` | `fn` for every event of `kinds` (an iterable of kinds; `None`: every kind), whatever the levels |
| `unlisten(fn)` | removes `fn` from every kind |

Plugins reach them through `PluginContext.listen` / `unlisten` ([hooks](hooks.md)).

## Emitting

`Bus(knobs)` reads its level from `knobs["events"]` on every call. Emitters ask
`wants(kind, job=None)` before building an event: true when some listener listens to every kind
or to `kind`, or when a subscriber takes it now (below). An event nobody receives is never
built. (`active`, true when the bus has any subscriber or listener, is kept but no emitter
reads it.)

`emit(kind, job=None, **fields)`:

1. `counts[kind] += 1`; `job` is added to the fields when given.
2. Every listener of every kind and of `kind` is called; a listener that raises is logged and
   removed (`unlisten`).
3. The machine-wide level: without subscribers, at `"counters"` for a kind outside
   `CORE_EVENTS`, or at `"sampled"` for a non-core kind of a job not sampled, it stops here.
4. One `Event(kind, t=time.time(), fields)`, built for the first subscriber that takes it, is
   offered to every subscriber that takes it.

A job's sampling is set at start with `bus.sample(job.id, job.sampled)`: the job's record choice
when `record` is set, else a draw with probability `event_sample`, so a recorded job also has its
events. `forget_job` drops it when the job ends. Events with no job are always sampled in.

`CORE_EVENTS` are delivered at every level: `job.admit`, `job.end`, `unit.up`, `unit.down`,
`unit.attach`, `unit.detach`, `unit.detached`, `lane.dead`, `plugin.disabled`,
`snapshot.error`, `fault.taken`, `machine.start`, `machine.stop`, `machine.error`, `counters`.

## Subscribers

`subscribe(plugin)` creates a `_Sub`:

| From the plugin | Default | Meaning |
|---|---|---|
| `events` | — | the subscriber's declared level: `"all"`, `"sampled"` or `"counters"` (`LEVELS`) |
| `overflow` | `"drop"` | what a full queue does (`OVERFLOW`) |
| `queue_size` | 10 000 | queue bound |

`takes(core, sampled)` first returns the subscriber to its declared level once the queue holds
at most a quarter of its bound, then filters by the current level: `"counters"` takes core
events (which include `counters`), `"sampled"` core events and events of sampled jobs, `"all"`
everything.

`offer(event)` puts the event without blocking; on a full queue it is dropped and counted
(`dropped`), and the overflow policy lowers the current level until the queue drains:

| `overflow` | On a full queue |
|---|---|
| `drop` | keep the level |
| `sample` | `"sampled"` |
| `counters_only` | `"counters"` |

An event the lowered level skips but the declared level would take is counted in `dropped`
too (`skips`). `Bus.dropped()` gives the counts by plugin name, including subscribers that
unsubscribed (`gone_dropped`).

The thread `togeari-sub-<name>` calls `plugin.on_event(event)` per event; exceptions are logged.
`unsubscribe(plugin, deadline=None)` and `Bus.close` (at `Machine.stop`) put a `None` end marker
behind the queued events and join the thread, both by one monotonic deadline: the one the
plugin manager passes (a plugin with `close_s`: `close_s` from the start of its unload, shared
with its `on_unload` drain), else the plugin's `close_timeout_s` (60 s) from now. Events still
queued at the deadline are dropped and counted, and the thread ends after its current event.
`unsubscribe(plugin, wait=False)` (a plugin disabled
by an error or by `plugin_disable`, on the dispatcher thread) returns at once: the queued events
are dropped and counted in `dropped`, the end marker is posted, and the thread is joined in
`Bus.close`.

## Events emitted

`stmt.*` events go through `IssueStage.emit_stmt`, which adds `node`, `addr`, `key` (the order
key) and `lane` (`None` for inline nodes).

| Kind | Emitted by | Fields |
|---|---|---|
| `job.admit` | `JobsStage.start_job` | `job`, `key`, `entry` |
| `job.end` | `JobsStage.end_job` | `job`, `key`, `ok`, `entry`, `failure`, `stats` |
| `stmt.fetch` / `stmt.ready` | `admit_inflight` / `check` | stmt fields |
| `stmt.issue` | `execute` (lane path) | stmt fields |
| `stmt.done` | `complete` | stmt fields, `unit` (name, `"store"` or `"inline"`) |
| `stmt.retire` / `stmt.squash` | `retire_one` / `squash` | stmt fields |
| `strand.park` | `ControlOps.park` | `job`, `strand`, `addr`, `cont` (the continuation strand) |
| `strand.resume` | `WritebackStage.wake` | `job`, `strand`, `addr` |
| `strand.end` | `FrontEnd.end` | `job`, `strand`, `addr` |
| `fault.taken` | `take_fault` | `job`, `addr`, `squashed`, `error_kind`, `error_node`, `error_message` |
| `lane.enqueue` | `execute` | `job`, `lane`, `node` |
| `lane.submit` | `Lane._submit` | `lane`, `unit`, `items` |
| `lane.result` | `deliver` | `job`, `node`, `addr`, `lane`, `unit`, `queued_s`, `service_s`, `result` |
| `lane.dead` | `Lane.timers` | `lane`, `items` |
| `unit.up` / `unit.down` / `unit.attach` / `unit.detach` / `unit.detached` | `Lane` (`Health`) | `lane`, `unit`, `reason` |
| `store.error` | `IssueStage.store_error` | `op`, `error` |
| `snapshot.error` | `RetireStage.take_snapshot` | `job`, `key`, `error` |
| `plugin.hook` | `PluginManager._time` | `plugin`, `hook`, `time_s`, `blocked` |
| `plugin.error` / `plugin.disabled` | `PluginManager.failed` | `plugin`, `hook`, `error` |
| `dispatcher.cycle` | `LoopStage.cycle`, every 100 cycles | `cycles`, `phase_s` (cumulative) |
| `machine.start` / `machine.stop` | `Machine` | — |
| `machine.error` | `LoopStage.died` | `error` |
| `counters` | the counters plugin, through `PluginContext.emit` | its snapshot |

The `job` field is the job's numeric id; `key` is the order key in `stmt.*` events and the job
key in `job.*` and `snapshot.error` events.
