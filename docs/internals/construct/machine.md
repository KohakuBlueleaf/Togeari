# Machine construction

`Machine` builds every part from the instances it is given, collects every problem into one
`ConstructionError` (`src/togeari/core/construct/errors.py`), and owns the threads of a run. It
is the package `src/togeari/core/construct/machine/`, one mixin per concern:

| Module | Class | Holds |
|---|---|---|
| `build.py` | `Machine` (`LoadStage`, `UnitStage`, `RunStage`), `no_unit_start` | the constructor |
| `loading.py` | `LoadStage`, `Staged` | staging and committing programs, `load_now` |
| `units.py` | `UnitStage`, `start_units_of`, `adapter_identity` | units while running, lane wiring and events |
| `running.py` | `RunStage` | the dispatcher thread, `run`, `submit`, queries, commands, `stop` |

The knob registry is in `src/togeari/core/construct/knobs.py`; unit start and stop in
`src/togeari/core/construct/units.py`.

## Arguments

| Argument | Default | Meaning |
|---|---|---|
| `programs`, `program`, `entry` | — | `{program: [entries]}`; `program=` / `entry=` add one; `entry` is also the default entry |
| `libraries`, `sig_path`, `passes` | — | passed to [job programs](../program/job-programs.md) |
| `bind` | — | node-name patterns to `Use` / `Inline` ([binding](../program/binding.md)) |
| `admission`, `issue` | `Fixed(64)`, `OldestJobFirst()` | policy knobs; an instance or a contributed policy's name |
| `store` | `None` | results store and snapshot store |
| `plugins` | `[]` | plugin instances |
| `adapter`, `protocol` | `PythonAdapter()`, `"python"` | `Defaults`: the adapter of a `Use` without one, the protocol of a unit without one |
| `start_timeout_s` | 120 | per unit start |
| `start_units` | `True` | `False`, or a `no_unit_start()` block, checks everything but starts no unit |
| `source_buffer`, `sink_buffer` | 1024, 1024 | bounds of the source and sink queues |
| `seed` | 0 | record and event sampling RNGs |
| `core` | `None` | `"auto"`, `"rust"` or `"python"`; `None` reads `TOGEARI_CORE`, else `"auto"` ([native core](../native/README.md)) |
| `**knobs` | — | registry entries |

## Constructor steps

0. **Core.** `resolve(core)` (`core/construct/core.py`) sets `Machine.core`; a core that
   cannot run is a construction problem.
1. **Plugins.** A `Bus` over the knob dict, the `QUERIES` / `COMMANDS` tables,
   `PluginManager.add` per plugin, then `collect_policies()` (every `contribute_policies()`,
   before anything is bound).
2. **Knobs.** Every given knob, plus `admission` and `issue`, goes through
   `checked(name, value, contributed_policies)`; a problem is a construction problem.
   Without a machine `store`, a `Use(..., store=...)` binding and a set `snapshot_every` are
   construction problems.
3. **Programs.** Per program, `_stage` then `_commit` (below). The default entry is `entry=` or
   the first entry loaded.
4. **Unused patterns.** A `bind` pattern no node used is a warning.
5. **First raise.** `problems.raise_if_any()`; warnings are kept in `machine.warnings`.
6. **Units.** `Dispatcher(self, seed)`. Unless `start_units=False` or inside `no_unit_start()`,
   `start_units_of` starts every unit; any failure stops every unit and raises
   `ConstructionError` listing each failure under its lane. `units_started` records the outcome.
7. **Wiring.** `_wire(lane)` per lane; `plugins.load_all()`.

```
ConstructionError: 2 problems
  knobs          window must be an int >= 1, not 0
  demo.kir:2     node 'nope' is unknown
```

## Staged loading

A load never becomes visible half done.

| Method | Does |
|---|---|
| `_stage(prog, entries, problems)` | `build_job_programs`; a program another load in progress stages sets `Staged.after` and stops; otherwise per job program the cached region, or `CodeMemory.stage` + `bind_region` into a `Staged(entries, lanes)`; entry names (a clash with a committed entry becomes `<stem>.<name>`; a name another load in progress stages is a problem); `_check_lanes` on the new lanes |
| `_commit(staged)` | `CodeMemory.commit` per region; new lanes appended and wired; entries registered |
| `lane_of(target, staged, problems)` | a `Lane` as is; an engine: the lane already holding it, or a new one-unit lane (a batch lane takes the contributed `default_former`); new lanes get `_resolve_policies` (a `former` / `select` given as a string names a contributed policy; one the lane refuses when set, `TypeError` or `ValueError`, is a problem) |
| `_check_lanes(lanes, problems)` | kind `mixed`; batch lane without former; non-batch lane with former; `max_inflight <= 0`; two different unit objects with one name anywhere in the machine |
| `load_now(program, entries)` | the `load` command (below); returns a `Future` of the entry names |

`load_now` while running:

1. `_stage`; a program staged by another load in progress is loaded again once that load ends
   (`_load_after`).
2. Problems raise `ConstructionError`, after `_discard` drops the load's new regions from code
   memory (`CodeMemory.discard`: a region never committed leaves when its instructions are the
   tail of memory, newest first).
3. Without new units (or units not started) the load commits at once.
4. Otherwise its programs (`_loading`, by semantic hash) and entry names (`_loading_names`) are
   held, the new units start off the dispatcher thread, and on the dispatcher the load commits
   (or the units stop, the new regions are discarded and the `Future` fails); the holds are
   released either way.

`Machine.load` waits `start_timeout_s + REPLY_SLACK_S` (30 s) for the reply.

## Knob registry

`machine.knobs` is one dict. Components read it on every use; `set_knob` validates through the
same `checked` function.

| Knob | Default | Valid values | Read by |
|---|---|---|---|
| `window` | 64 | int ≥ 1 | `FrontEnd.blocked_by_caps` |
| `retire_buffer` | 4096 | int ≥ 1 | `FrontEnd.blocked_by_caps` |
| `run_ahead` | `True` | bool | `ControlOps.control_value` |
| `mutates` | `"copy-if-speculative"` | `MUTATES_MODES` | `IssueStage.gate` |
| `inline_warn_us` | 50 | None or ≥ 0 | `IssueStage.note_inline` |
| `snapshot_every` | `None` | None or int ≥ 1 | `RetireStage.retire`, `JobsStage.start_job` |
| `record` | `None` | None or a rate in [0, 1] | `JobsStage.start_job` |
| `record_limit` | 1 000 000 | None or int ≥ 1 | `RetireStage.retire_one` |
| `events` | `"sampled"` | `LEVELS` | `Bus.emit` |
| `event_sample` | 0.1 | rate in [0, 1] | `JobsStage.start_job` (jobs sampled for events while `record` is off) |
| `fingerprint_inputs` | `False` | bool | issue, writeback |
| `assert_alias` | `False` | bool | retire, finish |
| `step_budget` | `None` | None or int ≥ 1 | `FrontEnd.run` |
| `plugin_errors` | `"disable"` | `"disable"`, `"log"` | `PluginManager.failed` |
| `gc_threshold` | `(50000, 20, 100)` | None or a tuple (a list is taken as one) of 1 to 3 ints ≥ 0, the first ≥ 1 | `LoopStage.apply_collector` |
| `squash_reuse` | 1024 | int ≥ 0 | `FaultStage.keep_for_reuse`, `IssueStage.issue_one` |
| `admission` | `Fixed(64)` | has `allows`, or a contributed policy's name | `JobsStage.admit` |
| `issue` | `OldestJobFirst()` | has `order` and `priority`, or a name | issue |

## Units

`start_all(units, timeout)` starts each distinct unit on its own `togeari-start-<name>` thread
(`start()`, then `ready()` every 50 ms) and waits at most `timeout` in total; it returns
`(unit, problem)` per failure, a hung `start()` reported as timed out. `stop_all(units)` calls
`stop()` on each distinct unit and logs failures.

While running, units start off the dispatcher thread: `_start_off_thread(units, then, fut)` runs
`start_units_of` on a `togeari-start` thread and posts `("call", then(failures), None)` to the
inbox; a `then` that raises is counted as a machine error and fails `fut`.

| Method | Does |
|---|---|
| `attach_now(lane, unit)` | the `attach` command: `_attach_problems` (the unit object already attached, a name in use, a kind different from the lane's, `check_unit` for every node bound to the lane) raise `ConstructionError`; then start off-thread and, once started, `lane.attach(unit)` and index it; a `Future` of the unit name (`Machine.attach` waits `start_timeout_s + 30` s) |
| `detach_now(unit, drain)` | `lane.detach(...)`; returns the re-queued count |
| `lane_event(lane_name, kind, **fields)` | the bus event, with `lane` set to the lane's name; `unit.detached` stops the unit off-thread when no lane holds it (`_stop_detached`); every `unit.*` event calls `on_unit_state(unit, state)` with the lane as its gate |
| `_wire(lane)` | `on_done`, `on_wake`, `hook_submit`, `emit`, `wants` ([lane](../lanes/lane.md#wiring-and-events)) |
| `identity(adapter, lane)` | `(adapter_identity(adapter), lane.engine_identity())` for content store keys |

`adapter_identity` is `repr(adapter.spec())` when the adapter has one, else its class and the
bound function's qualified name.

## Running

| Method | Does |
|---|---|
| `_ensure_thread()` | raises `DispatcherDied` after a death, `RuntimeError` when stopped or built without starting units; starts `togeari-dispatcher` (with `on_machine_start`, `machine.start`) |
| `_post(ev)` | puts on the inbox; after a death the inbox is refused at once ([supervision](../runtime/loop/supervision.md)) |
| `run(source, sink, limit=None)` | below |
| `submit(inputs, key=None, entry=None)` | `("submit", SourceItem, JobHandle)`; a missing key becomes `job-<n>` with `auto_key=True` |
| `inspect` / `command` | `Reply` futures ([queries and commands](../plugins/queries-and-commands.md)) |
| `load`, `attach`, `detach` | blocking wrappers over the commands |
| `signals()` | `Dispatcher.signals()` directly on the dispatcher thread or when it is not running, else the `signals` query |
| `stop(drain=True, timeout=60.0)` | posts `("stop", drain, Reply)` and joins the dispatcher for `timeout` (`STOP_CANCEL_S` = 10 s at most without drain); then a non-draining stop and at most 10 s more; a dispatcher still running is abandoned (`Dispatcher.abandon`: marked dead, every caller answered with `DispatcherDied`); then `on_machine_stop`, `machine.stop`, unload plugins, close the bus (each subscriber's queue is delivered first), `stop_all` units |

`run` starts two threads around one `RunState`:

| Thread | Does |
|---|---|
| `togeari-source` (`_read`) | iterates the source into `buf` (`Queue(source_buffer)`), checking `limit`, a death and a stop before taking each item; a plain mapping becomes `SourceItem(str(next(_keys)), dict(item), auto_key=True)`, its key a unique string from the process-wide counter `_keys` (shared with `submit`'s `job-<n>` keys), not a position; after a put it posts `("wake",)` when `d.source_want` is set; at the end sets `run.source_finished` and posts `("wake",)`; an exception goes to `run.source_error` |
| `togeari-sink` (`_write`) | takes records from `sink_q` (`Queue(sink_buffer)`) and writes them; after a take it posts `("wake",)` when `d.sink_wait` is set; a write error goes to `run.sink_error` and later records are skipped |

The run is attached with a `("run", buf, sink_q, run)` event through `_post`. `run` waits on
`run.done`, joins the reader (after a death it empties `buf` meanwhile, so a reader blocked on a
full buffer can finish), ends the writer with `SINK_END`, closes the sink, then raises
`DispatcherDied`, the source error or the sink error, in that order, or returns
`summary(seconds, stopped)`.
