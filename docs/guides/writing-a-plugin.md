# Writing a plugin

A plugin changes or observes what happens between the machine's parts. Subclass `BasePlugin`
and implement any hooks; every hook is optional. The code blocks on this page run in order as
one script.

```python
from collections import Counter

from togeari import BasePlugin, PluginBlockError
from togeari.policies import Fixed


class SkipEmpty(BasePlugin):
    name = "skip_empty"
    priority = 10
    events = "all"

    def __init__(self):
        self.failed = []
        self.kinds = Counter()

    def pre_admit(self, item):
        # Jobs without items never start: they reach the sink as Blocked failures
        if not item.inputs.get("items"):
            raise PluginBlockError("no items")
        return None

    def on_job_end(self, record):
        # Every record, rejected items included
        if not record.ok:
            self.failed.append((record.key, record.failure["kind"]))

    def on_event(self, event):
        self.kinds[event.kind] += 1

    def contribute_queries(self):
        return {"skipped": lambda machine: list(self.failed)}

    def contribute_policies(self):
        return {"wide": Fixed(256)}
```

```python
from togeari import Inline, Machine
from togeari.io import Iterable, Memory

plugin = SkipEmpty()
machine = Machine(
    program="@def (items: list)main(n):\n    (items)len(n)\n",
    entry="main",
    bind={"kir.std.*": Inline()},
    admission="wide",
    plugins=[plugin],
)
with machine:
    sink = Memory()
    machine.run(Iterable([{"items": [1, 2]}, {"items": []}]), sink)
    print(sorted((r.key, r.ok, r.outputs) for r in sink.records))
    print(machine.inspect("skipped").result())
    print(machine.inspect("plugins").result())
print(plugin.kinds["job.end"], "job.end events")
```

Load a plugin with `Machine(plugins=[...])`, or while running with
`machine.command("plugin_add", plugin=...)`. `plugin_enable` / `plugin_disable` (by name) turn
it on and off. Disabling also removes the queries, commands, services and policies it
contributed (an entry another plugin replaced since stays); enabling runs `on_load` and
contributes them again.

## Hooks

Hooks run in priority order (lower first). Hot-path hooks run on the dispatcher thread and must
be cheap; heavy work belongs in `on_event` or the plugin's own thread.

| Hook | Fires | Arguments | Return / raise |
|---|---|---|---|
| `pre_admit(item)` | before a source item becomes a job | `SourceItem` | a new `SourceItem` (rewrite inputs, choose the entry), `None` to keep; `PluginBlockError`: the item reaches the sink as a `Blocked` failure |
| `vote_admit(signals)` | each admission check while jobs are resident | the admission signals | `False` stops admitting for now |
| `on_job_start(job)` | a job was admitted | `{key, entry, inputs, recorded}` | — |
| `pre_submit(item, unit=, lane=)` | after unit choice, before `submit` | `WorkItem`, the unit, the lane name | a new `WorkItem` (it keeps the machine's bookkeeping), `None`; `PluginBlockError`: the item fails with `ItemError(Blocked)` |
| `post_result(result, node=, job=)` | an engine or inline result enters writeback | `Ok` or `ItemError`, node name, job key | a new `Ok` / `ItemError`, `None`; the recorded result is the one after this hook |
| `on_fault(info)` | a fault is taken (caught or not) | `{job, key, kind, node, message}` | — |
| `on_job_end(record)` | a job ended, or an item was rejected before it became a job | `JobRecord` | — |
| `pre_sink(record)` | before any record (a job's or a rejected item's) reaches its sink or handle | `JobRecord` | a new `JobRecord`, `None`; `PluginBlockError`: the record is not written (a waiting handle still gets it) |
| `on_unit_state(unit, state)` | a unit changed state | unit name; `"up"`, `"down"`, `"attach"`, `"detach"`, `"detached"` | — |
| `on_load(ctx)`, `on_unload()` | enabled / disabled, machine stop; `on_unload` also runs after an `on_load` that raised (the plugin then stays disabled, its contributions withdrawn); with `close_s` set, the machine stop unloads the plugin by one deadline, `close_s` from the start of its unload, shared by the delivery of its queued events and its own drain (`self.unload_by()` gives the deadline) | `PluginContext` | — |
| `on_machine_start()`, `on_machine_stop()` | dispatcher start, `machine.stop()` | — | — |
| `on_event(event)` | every bus event the plugin receives | `Event(kind, t, fields)` | on the plugin's own thread |
| `contribute_queries()`, `contribute_commands()` | at load | — | `{name: fn(machine, **args)}` |
| `contribute_policies()` | at construction, before programs are bound | — | `{name: policy}`: `admission=`, `issue=`, a lane's `former=` / `select=` and `set_knob` may name them |
| `runtime_services()` | at load | — | `{name: object}` for `ctx.service(name)` |

A transform that returns a value of the wrong type, or any hook that raises (other than a
block), is a plugin error: it is logged, its change is skipped, a `plugin.error` event is
emitted, and with the `plugin_errors` knob at `"disable"` (the default) the plugin is disabled
(`plugin.disabled`). With `"log"` it stays enabled. The machine goes on either way.

**Gates.** `applies_to={"entries": [...], "lanes": [...]}` limits a plugin to jobs of those
entries and to those lanes (hooks without a job or lane are not gated). `should_apply(ctx)` is
a dynamic gate evaluated before each call.

## Events

Set `events` to `"all"`, `"sampled"` or `"counters"` to receive bus events in `on_event`,
through a bounded queue (`queue_size`, 10 000) drained on the plugin's own thread. `overflow`
says what a full queue does: `"drop"` (count and drop), `"sample"` or `"counters_only"` (lower
the plugin's level until the queue drains). The machine-wide `events` knob caps every
subscriber; detail events are sampled per job (`record` choice, else `event_sample`). A
`"counters"` subscriber receives the always-delivered events below. At `Machine.stop` every
queued event is delivered before the plugin unloads.

| Events | Fields |
|---|---|
| `job.admit`, `job.end` | job, key, entry; `job.end` adds ok, failure, stats |
| `stmt.fetch`, `stmt.ready`, `stmt.issue`, `stmt.done`, `stmt.retire`, `stmt.squash` | job, node, addr, key (order key), lane; `stmt.done` adds unit |
| `strand.park`, `strand.resume`, `strand.end` | job, strand, addr |
| `fault.taken` | job, addr, key (order key), squashed, error_kind, error_node, error_message |
| `lane.enqueue`, `lane.submit`, `lane.result`, `lane.dead` | lane, unit, items, queue and service times; `lane.submit` adds work (each item's job key, addr, order key) |
| `unit.up`, `unit.down`, `unit.attach`, `unit.detach`, `unit.detached` | lane, unit, reason |
| `store.error`, `snapshot.error` | the error |
| `plugin.hook`, `plugin.error`, `plugin.disabled` | plugin, hook, time or error |
| `dispatcher.cycle` | cycles, cumulative time per phase (every 100 cycles) |
| `machine.start`, `machine.stop`, `machine.error` | — / the error |
| `counters` | the counters plugin's snapshot |

Always delivered: `job.admit`, `job.end`, every `unit.*` event, `lane.dead`, `fault.taken`,
`plugin.disabled`, `snapshot.error`, `machine.start`, `machine.stop`, `machine.error` and
`counters`. The rest, including `lane.enqueue`, `lane.submit` and `lane.result`, follow the
level.

## The context

`on_load(ctx)` receives a `PluginContext`:

| Member | Gives |
|---|---|
| `ctx.inspect(name, **args)`, `ctx.command(name, **args)` | futures, answered by the dispatcher between cycles |
| `ctx.post(fn)` | runs `fn()` on the dispatcher thread between cycles; an error in it is this plugin's error |
| `ctx.query_now(name, **args)` | answers a query directly (on the dispatcher thread, or when stopped) |
| `ctx.emit(kind, **fields)` | puts an event on the bus; from a thread other than the dispatcher's, the emit is posted and runs on the dispatcher thread |
| `ctx.service(name)` | another plugin's runtime service |
| `ctx.signals()`, `ctx.knobs`, `ctx.code` | admission signals, the knob registry (a copy), code memory |
| `ctx.listen(fn, kinds=None)`, `ctx.unlisten(fn)` | `fn(kind, fields)` called synchronously on the dispatcher thread for each event of `kinds` (None: every kind); it must be cheap and must not keep the fields' objects |
| `ctx.stats()`, `ctx.summary(seconds)` | a deep copy of the dispatcher's counters; the run totals |
| `ctx.lanes(loads=False, now=None)`, `ctx.units(loads=False)` | every lane and unit as dicts: state, in flight, rates, busy time, errors, configuration; with `loads`, each unit's `load()` |
| `ctx.event_counts()`, `ctx.events_dropped()` | events emitted by kind; events each subscriber's queue dropped |
| `ctx.unit_specs()`, `ctx.construction()`, `ctx.libraries`, `ctx.sig_path` | how the machine was built |
| `ctx.resident_jobs()`, `ctx.hook_seconds()`, `ctx.machine_running()` | resident jobs; seconds spent in each plugin's hooks; whether the dispatcher runs |

The reads of machine state (`stats`, `summary`, `lanes`, `units`, `resident_jobs`,
`hook_seconds`, `event_counts`) are answered on the dispatcher thread: call them from a hook,
inside `ctx.post(fn)`, or from a query, not from the plugin's own threads. Each returns a copy
the plugin owns. The builtin plugins use only the context.

## Queries and commands

Queries: `jobs`, `job(id)`, `lanes`, `units`, `programs`, `signals`, `plugins`, `knobs`,
`stats`. Commands: `attach`, `detach`, `cancel_job`, `pause_job`, `resume_job`, `set_knob`,
`set_lane_policy`, `load`, `plugin_add`, `plugin_enable`, `plugin_disable`. Plugins add their
own.

The `lanes` query and `ctx.lanes()` have different shapes:

| Read | Each lane | Each unit |
|---|---|---|
| `lanes` query | `name`, `kind`, the lane's signal fields spread flat (`queue`, `inflight`, `units_up`, `waiting`), `stats`, `batch_sizes`, `units` | `name`, `state`, `inflight`, `rate`, `done`, `errors` |
| `ctx.lanes()` | `name`, `kind`, `config`, `signals` (nested), `stats`, `batch_sizes`, `units` | the query's fields plus `lane`, `busy_s`, `submit_s`, and `load` with `loads=True` |

## Builtin plugins

| Plugin (`togeari.plugins`) | Does |
|---|---|
| `Counters(interval_s=1.0)` | per-interval counters of the dispatcher, lanes, units, jobs (resident jobs by state: `executing`, `queued`, `waiting`, `parked`, and `job_seconds` per state: pool-wide, each sampled count times the interval) and work; the `counters` query and event |
| `RunReport(path)` | `spec.json`, `env.json`, `metrics.jsonl`, `events.jsonl[.zst]`, conformance records, `summary.json` (rewritten with every counters snapshot, never dropped under an event flood, and at unload after the writer stops by the unload's deadline; with weak digests by type, `weak_digest_count`, `store_weak`, and `weak_jobs` / `weak_values` of the records) |
| `Perfetto(path, sample_s=0.25, close_s=10.0)` | a trace file with tracks per dispatcher phase, lane queue, unit and job; at unload the writer drains by the unload's deadline, then the array closes |
| `Recorder(path, close_s=10.0)` | conformance records of recorded jobs, for `togeari conform`; at unload the writer drains by the unload's deadline, then the record files close |
| `Inspector(host="127.0.0.1", port=0, token=<random>, allowed_hosts=())` | HTTP: `GET /queries/<name>`, `POST /commands/<name>`, `GET /events` (server-sent events) |
| `Budget(max_jobs=, max_calls_per_node=, walltime_s=)` | stops admitting and rejects items past its caps; blocks submits past a node's call cap |

**Inspector access.** The `Host` header must be `localhost`, `127.0.0.1`, `[::1]`, the bound
host or one of `allowed_hosts`, with the bound port; an `Origin` header, when present, must be
`http://` and one of those; a command body must be `application/json`; every request carries
the access token as `Authorization: Bearer <token>`, `X-Togeari-Token` or a `token` query
argument. Without a `token` argument the inspector makes a random one: read it from
`inspector.token`, or open `inspector.access_url` (the address with `?token=` added).
`token=None` turns the check off, for a loopback-only machine whose local users are trusted.
`port=0` (the default) picks a free port; `inspector.url` gives the bound address (an IPv6 host
such as `"::1"` appears as `[::1]`). A refused request is answered without parsing its
body; a body up to 64 KiB is drained first. A command body over `max_body` (1 MiB) is answered
413, and an invalid `Content-Length` 400.

**Inspector commands.** The inspector adds two commands of its own: `inspector_end_streams`
ends every open event stream (a dashboard reconnects to get a fresh one) and answers how many;
`inspector_token` with `token` (a string, or `None` for no token) replaces the access token for
every later request and answers `{"token_required": ...}`. The `inspector` query tells whether
a token is required, never the token.
