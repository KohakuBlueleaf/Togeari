# Plugin manager and hooks

`BasePlugin`, `PluginContext`, `PluginBlockError` (`src/togeari/core/plugin/base.py`) and
`PluginManager` (`src/togeari/core/plugin/manager.py`). How to write a plugin is in
[writing a plugin](../../guides/writing-a-plugin.md); this page is where and how each hook runs.

## Registry

| Method | Does |
|---|---|
| `add(plugin)` | appends; renames a duplicate name to `name#<n>`; sorts by `priority` (lower first); creates its `PluginContext` |
| `collect_policies()` | every plugin's `contribute_policies()` merged by name; called by `Machine` before any program is bound, so lanes, `admission=`, `issue=` and `set_knob` may name them |
| `load(plugin)` | `on_load(ctx)`; when it raises, the plugin's `on_unload` runs (releasing what `on_load` took), the plugin stays disabled and `load` returns `False`; otherwise enabled, `contribute_queries()`, `contribute_commands()`, `runtime_services()` merged into the machine's tables, a bus subscription when it has `events` and `on_event`, `rebuild()` |
| `enable(name)` / `disable(name)` | `load` again / remove from the hook lists, unsubscribe without waiting (queued events dropped), `on_unload` |
| `unload_all()` | every enabled plugin: unsubscribe, `on_unload` |
| `rebuild()` | `by_hook[hook]` = enabled plugins, in priority order, whose class overrides `hook` |

`HOOKS` lists the hooks with precomputed lists: `pre_admit`, `vote_admit`, `on_job_start`,
`on_job_end`, `pre_submit`, `post_result`, `on_unit_state`, `on_fault`, `pre_sink`,
`on_machine_start`, `on_machine_stop`. A hook with no plugin costs one empty-list lookup
(`has(hook)` guards `pre_submit` and `post_result`).

## Calling conventions

| Method | Kind | Semantics |
|---|---|---|
| `transform(hook, value, kind=None, where=None, **ctx)` | `pre_*`, `post_*` | each plugin gets the previous plugin's value with `**ctx`; `None` keeps it; a value of type `kind` replaces it; any other value is a plugin error; `PluginBlockError` propagates to the caller |
| `vote(hook, *args, where=None)` | `vote_*` | the first `False` stops; an error counts as yes |
| `notify(hook, *args, where=None)` | `on_*` | each plugin via `call` |
| `call(p, hook, *args)` | any | `(True, return value)` or, after an error, `(False, None)` |

**Plugin errors.** A hook that raises (other than a block), returns a value of the wrong kind,
or a `PluginContext.post` function that raises, goes to `failed(p, hook, exc)`: the plugin's
error count, a log line, a `plugin.error` event; with `knobs["plugin_errors"] == "disable"` the
plugin is disabled and `plugin.disabled` is emitted. With `"log"` it stays enabled. The machine
goes on either way.

**Timing.** Every call adds its time to `timing[plugin][hook]` (the `plugins` query) and, when
the bus wants it (`Bus.wants("plugin.hook")`), emits `plugin.hook` with `time_s` and `blocked`.

**Gates.** `applies(p, where)` runs before each call: `applies_to={"entries": [...]}` is compared
with `where["entry"]` and `{"lanes": [...]}` with `where["lane"]` when the call site gives them;
then `should_apply(ctx)` when the plugin overrides it (an error there is a plugin error and
skips the call).

## Call sites

All run on the dispatcher thread except `on_event`.

| Hook | Called from | Arguments | `where` | Block / veto |
|---|---|---|---|---|
| `pre_admit` | `JobsStage.start_job` | `SourceItem` (`kind=SourceItem`) | entry | the item is rejected, kind `Blocked` |
| `vote_admit` | `JobsStage.admit` | `signals` | — | stops admitting this cycle |
| `on_job_start` | `JobsStage.start_job` | `{key, entry, inputs, recorded}` | entry | — |
| `pre_submit` | `Lane._submit` through `before_submit`, after unit choice | `WorkItem` (`kind=WorkItem`); `unit=`, `lane=` (name) | lane, entry | the item fails with `ItemError(Blocked)` |
| `post_result` | `WritebackStage.complete` | a `Result`: `Ok`, `ItemError`, `UnitError` or `Cancelled`; `node=`, `job=` (key) | entry, lane | — |
| `on_fault` | `FaultStage.take_fault` (caught or not) | `{job, key, kind, node, message}` | entry | — |
| `on_job_end` | `JobsStage.end_job`, and `JobsStage.reject` for items rejected before they became jobs | `JobRecord` | entry | — |
| `pre_sink` | `JobsStage.emit_record` | `JobRecord` (`kind=JobRecord`) | entry | the record is not written; `records_blocked`; a waiting handle still gets it |
| `on_unit_state` | `Machine.lane_event` | unit name, `"up"` / `"down"` / `"attach"` / `"detach"` / `"detached"` | lane | — |
| `on_machine_start` | `Machine._ensure_thread` | — | — | — |
| `on_machine_stop` | `Machine.stop` | — | — | — |
| `on_event` | the plugin's bus thread | `Event` | — | — ([bus](bus.md)) |

A `pre_submit` replacement item keeps the original's machine bookkeeping (`owner`, `ext`). The
conformance record holds the result after `post_result`, the value the job used.

## Context

`PluginContext(machine, plugin)` is everything a plugin reaches; builtin plugins use only it.
Reads marked *dispatcher thread* are answered on the dispatcher thread (a hook, `post`, a query)
or while the machine is stopped; what they return is a copy the plugin owns.

| Member | Gives |
|---|---|
| `code` | the `CodeMemory` (spans through `Instr.span`) |
| `knobs` | a copy of the knob registry |
| `libraries`, `sig_path` | the machine's node libraries and signature path (copies) |
| `machine_running()` | whether the dispatcher thread runs; any thread |
| `for_plugin(plugin)` | a context of the same machine for a plugin run inside this one |
| `query_names()`, `command_names()` | the table names |
| `stats()` | a deep copy of the dispatcher's counters (dispatcher thread) |
| `summary(seconds)` | the machine's run totals (dispatcher thread) |
| `unit_specs()` | each unit's `spec()` by name (`{"error": repr}` when it raises) |
| `resident_jobs()` | jobs resident (dispatcher thread) |
| `job_states()` | resident jobs by `JOB_STATES` (dispatcher thread): `executing` (a call handed to a unit), `queued` (calls issued, none handed to a unit), `waiting` (statements waiting on values or a gate, strands stalled on the window), `parked` (paused, or strands parked with nothing unfinished); per job with issued calls, a scan in window order up to its first call handed to a unit or its last issued call |
| `hook_seconds()` | seconds spent in each plugin's hooks (dispatcher thread) |
| `event_counts()`, `events_dropped()` | events emitted by kind; events each subscriber dropped |
| `construction()` | `default_entry`, `passes`, `bind`, `store`, `contributed_policies` (names), `warnings` |
| `lanes(loads=False, now=None)` | every lane as a dict: `name`, `kind`, `config` (former, selector, forming, max in flight, retry, timeout, dead time), `signals`, `stats`, `batch_sizes`, `units`; busy spans measured up to `now` (dispatcher thread) |
| `units(loads=False)` | every unit as a dict: `name`, `lane`, `state`, `inflight`, `rate`, `done`, `busy_s`, `submit_s`, `errors`; with `loads`, `load` (dispatcher thread) |
| `listen(fn, kinds=None)`, `unlisten(fn)` | bus listeners on the dispatcher thread ([bus](bus.md#listeners)) |
| `signals()` | `Machine.signals()`: a copy, from any thread |
| `inspect(name, **args)`, `command(name, **args)` | `Reply` futures through the inbox |
| `post(fn)` | `("call", fn, plugin name)` on the inbox; `False` when the dispatcher thread is not running; an error in `fn` is this plugin's error |
| `query_now(name, **args)` | the query function called directly (dispatcher thread, or a stopped machine) |
| `emit(kind, **fields)` | `bus.emit`; from another thread while the machine runs, posted to the dispatcher thread |
| `service(name)` | a contributed runtime service |
