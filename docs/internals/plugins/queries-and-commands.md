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
| `lanes` | — | per lane: `name`, `kind`, `former` and `select` (type names; `former` None off batch lanes), `signals()`, `stats`, `batch_sizes`, units (`name`, `state`, `inflight`, `rate`, `done`, `errors`) |
| `units` | — | the units of every lane, as in `lanes` |
| `programs` | — | per region: `region`, `source`, `entry`, `hash` (16 hex), `addrs` `[start, end]`, `inputs`, `outputs` |
| `signals` | — | a deep copy of `Dispatcher.signals()` |
| `plugins` | — | `name`, `priority`, `enabled`, `errors`, `hook_s` (time per hook) |
| `knobs` | — | the registry; non-scalar values (policies) as their class name |
| `knob_specs` | — | `knobs`: per knob its spec from `KNOB_SPECS` with its `value` (as in `knobs`); `policies`: the contributed policies' names by the roles they fill (`admission`, `issue`, `former`, `select`), what `set_knob` and `set_lane_policy` take by name |
| `stats` | — | a deep copy of the dispatcher totals; `inline_s` as total seconds per node |
| `waits` | `job=None`, `limit=256` | version-1 detached causal metadata graph; numeric resident-job filter, total entity+relation bound, explicit truncation/unsupported capabilities |

## Knob specs

`KNOB_SPECS` (`core/construct/knobs.py`) says what each knob takes, for an editor: `kind` is
`int`, `number`, `rate` (a number in [0, 1]), `bool`, `choice` (one of `choices`), `ints` (1 to
3 ints, the first >= 1) or `policy` (a contributed policy of `role`); `min` is the lower bound
and `nullable` that None is accepted. `checked` is the rule; `tests/machine/test_knob_specs.py`
derives values each spec allows and rules out and checks `checked` agrees, for every knob.

## Wait snapshot schema

`core/observe/waits.py` owns the shared renderer. Python reads live runtime
metadata; native `_wait_state(job, limit)` supplies the same private metadata DTO.
Private WorkItem references are used only to inspect cached core lane state and
never appear in the result. No payload, adapter preparation, policy/selector or
engine-capacity callback is executed.

```json
{
  "schema": 1,
  "run_id": "machine-query-local-uuid32",
  "snapshot_id": 1,
  "job": null,
  "entities": [],
  "relations": [],
  "truncated": false,
  "unsupported": []
}
```

Entities have `id`, `kind`, `state` plus relevant instruction `addr`, `region`,
logical `key`, verbatim `origin`, and invocation/attempt metadata. Kinds include
`job`, `inflight`, `strand`, `slot`, `window`, `lane`, `checkpoint`, and `summary`.
All IDs have a run prefix; dynamic slot/inflight/strand tokens retain runtime IDs.
A strand at a future control PC has `phase="not_materialized"`, not a fake call.

Relations have `id`, `from`, `to`, `reason`, and `detail`; direction is waits-on.
Reasons are `input`, `control`, `effect`, `window`, `lane_capacity`, `batch_wait`,
`resource_budget`, `retry_backoff`, `checkpoint`, or `unknown`. Relation identity
uses endpoints/reason/port/gate, excluding changing remaining-time/counter details.
Every endpoint is included; omitted evidence produces a truncation summary rather
than dangling references. `limit` bounds entities and relations combined and also
bounds metadata traversal. Infrastructure origins are null.

Input/control slots are resolved through forwarding and older mutation versions
without heap cleanup. Producers come from actual window outputs/version slots;
phi and collection ownership comes from actual strand frames/spawners. Effect
and window predicates mirror inspection-safe readiness conditions, not scheduler
gates that change state. Ordinary pause and terminal resource rejection are not
checkpoint/resource waits. Cached estimates alone are not budget-blocking evidence.

The query is opt-in and intended for dispatcher-safe inspection, not normal-cycle
or counters-only work. See [Explaining waits](../../guides/explaining-waits.md)
for interpretation and limitations.

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
