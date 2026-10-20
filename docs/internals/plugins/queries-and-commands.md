# Queries and commands

Two name-to-function tables, `machine.queries` and `machine.commands`, seeded from `QUERIES` and
`COMMANDS` in `src/togeari/core/plugin/tables.py` and extended by `contribute_queries()` /
`contribute_commands()`. Each function is `fn(machine, **args)`.

`machine.inspect(name, **args)` and `machine.command(name, **args)` post `("query" | "command",
name, args, Reply)` to the inbox. The dispatcher calls the function between cycles and sets the
reply; `Reply.result(timeout=30.0)` returns the value or raises the function's exception (a
`KeyError` for an unknown name). A function may return a `concurrent.futures.Future` for work
done off the dispatcher thread; the reply follows it. Queries answer copies. Nothing outside the
dispatcher reads or writes job state.

## Queries

| Name | Args | Returns |
|---|---|---|
| `jobs` | — | per resident job: `id`, `key`, `entry`, live `strands`, `window` length, `unfinished`, `paused` |
| `job` | `id` | `id`, `key`, `entry`, `inputs` (a `reprlib` repr bounded to 200 characters per value, without building the full repr), live strands (`id`, `state`, `pc`, `at` span, `key`, `spawner`), window entries (`node`, `at`, `key`, `state`, `unit`), `slots` (ready / pending over the slots named by any live strand, each once), `stats` (deep copy) |
| `lanes` | — | per lane: `name`, `kind`, `signals()`, `stats`, `batch_sizes`, units (`name`, `state`, `inflight`, `rate`, `done`, `errors`) |
| `units` | — | the units of every lane, as in `lanes` |
| `programs` | — | per region: `region`, `source`, `entry`, `hash` (16 hex), `addrs` `[start, end]`, `inputs`, `outputs` |
| `signals` | — | a deep copy of `Dispatcher.signals()` |
| `plugins` | — | `name`, `priority`, `enabled`, `errors`, `hook_s` (time per hook) |
| `knobs` | — | the registry; non-scalar values (policies) as their class name |
| `stats` | — | a deep copy of the dispatcher totals; `inline_s` as total seconds per node |

## Commands

| Name | Args | Does | Returns |
|---|---|---|---|
| `attach` | `lane`, `unit` | `Machine.attach_now`: checks, start off-thread, `lane.attach` ([machine](../construct/machine.md)) | a `Future` of the unit name |
| `detach` | `unit`, `drain=True` | `Machine.detach_now` ([unit health](../lanes/health.md)) | re-queued item count |
| `cancel_job` | `id` | `cancel_job`: fails the job with kind `Cancelled`, every output missing | `bool` |
| `pause_job` | `id` | `job.paused`: no fetch, ready inflights held, no parking | `bool` |
| `resume_job` | `id` | clears `paused`, moves held inflights back to `ready`, marks the job dirty | `bool` |
| `set_knob` | `name`, `value` | validates with `checked` (a policy knob may name a contributed policy); unknown names raise `KeyError`, invalid values `ValueError` | the old value |
| `set_lane_policy` | `lane`, `policy` (`"former"` or `"select"`), `value` | a string names a contributed policy; a former is set exactly on batch lanes, a selector is never None; the lane's setter checks the contract (`TypeError` answered as `ValueError`) and re-buckets queued items under a new former; an unknown lane raises `KeyError`, any refusal `ValueError` and keeps the old policy | the type name of the policy replaced |
| `load` | `program`, `entries` | `Machine.load_now` (staged; committed when every check passes and every new unit started) | a `Future` of the entry names |
| `plugin_add` | `plugin` | `PluginManager.add` and `load`; a failing `on_load` raises | plugin name |
| `plugin_enable` / `plugin_disable` | `name` | `PluginManager.enable` / `disable` | `bool` |

`Machine.load`, `attach`, `detach` and `signals` are blocking wrappers over these.
