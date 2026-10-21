# Internals

How the machine is built, file by file. The user-facing picture is in
[concepts](../concepts/README.md); the guides ([machine](../guides/writing-a-machine.md),
[engine](../guides/writing-an-engine.md), [plugin](../guides/writing-a-plugin.md),
[tuning](../guides/tuning.md)) cover usage. These pages describe the code under
`src/togeari/core/`, the builtin call engines, the inspector and the checks.

## Reading order

| # | Page | Code |
|---|---|---|
| 1 | [Machine construction](construct/machine.md) | `core/construct/machine/{build,loading,units,running}.py`, `core/construct/{knobs,units,errors}.py` |
| 2 | [Job programs](program/job-programs.md) | `core/program/job.py` |
| 3 | [Code memory](program/code-memory.md) | `core/program/code.py`, `core/program/decode/{instr,compiler}.py` |
| 4 | [Decode tables](program/decode-tables.md) | `core/program/decode/analysis.py` |
| 5 | [Binding](program/binding.md) | `core/program/bind.py`, `core/contracts/adapter.py` |
| 6 | [Job state](runtime/state.md) | `core/runtime/state.py` |
| 7 | [Dispatcher cycle](runtime/loop/cycle.md) | `core/runtime/{dispatcher,loop}.py` |
| 8 | [Supervision](runtime/loop/supervision.md) | `core/runtime/{loop,handles}.py` |
| 9 | [Jobs and records](runtime/pool/jobs.md) | `core/runtime/pool/jobs.py` |
| 10 | [Admission, source and sink](runtime/pool/admission.md) | `core/runtime/pool/{jobs,signals}.py`, `core/runtime/values.py` |
| 11 | [Fetch and order keys](runtime/front-end/fetch-and-keys.md) | `core/runtime/frontend/fetch.py`, `core/runtime/kir.py` |
| 12 | [Run-ahead](runtime/front-end/run-ahead.md) | `core/runtime/frontend/control.py` |
| 13 | [`each`](runtime/front-end/each.md) | `core/runtime/frontend/each.py` |
| 14 | [Issue and writeback](runtime/back-end/issue-and-writeback.md) | `core/runtime/backend/{issue,writeback}.py` |
| 15 | [`ordered` and `mutates`](runtime/back-end/effects.md) | `core/runtime/backend/issue.py`, `core/runtime/state.py` |
| 16 | [Window and retire](runtime/retire/window-and-retire.md) | `core/runtime/retire/retire.py` |
| 17 | [Faults](runtime/retire/faults.md) | `core/runtime/retire/faults.py` |
| 18 | [Snapshots](runtime/retire/snapshots.md) | `core/runtime/retire/snapshot.py` |
| 19 | [Lane](lanes/lane.md) | `core/lanes/{lane,pump}.py` |
| 20 | [Lane queues](lanes/queue.md) | `core/lanes/queue.py` |
| 21 | [Unit health](lanes/health.md) | `core/lanes/health.py` |
| 22 | [Admission and issue order](policies/admission-and-issue.md) | `core/policies/{admission,issue}.py`, `core/contracts/views.py` |
| 23 | [Forming and unit choice](policies/forming-and-selection.md) | `core/contracts/policies.py`, `core/policies/{former,select}.py` |
| 24 | [Plugin manager and hooks](plugins/hooks.md) | `core/plugin/{base,manager}.py` |
| 25 | [Queries and commands](plugins/queries-and-commands.md) | `core/plugin/tables.py` |
| 26 | [Event bus](plugins/bus.md) | `core/observe/bus.py` |
| 27 | [Inspector](plugins/inspector.md) | `plugins/serve/inspector.py` |
| 28 | [Python call engine](engines/python.md) | `engines/python/`, `engines/common/{ticket,registry}.py` |
| 29 | [Web call engine](engines/web.md) | `engines/web/` |
| 30 | [Conformance](verification/conformance.md) | `conformance/`, `core/contracts/digest.py`, `plugins/record/` |
| 31 | [Generated programs](verification/generated-programs.md) | `tests/machine/generated/`, `tests/conftest.py`, `tests/scaling/`, `testing/sim.py` |
| 32 | [Engine checks](verification/engine-checks.md) | `testing/check.py` |
| 33 | [Decision logs and traces](verification/trace.md) | `testing/trace/`, `tests/testing/trace/` |
| 34 | [Native core](native/README.md) | `src/togeari-rust/`, `core/construct/core.py`, `Cargo.toml` |
| 35 | [Native dispatcher](native/core.md) | `src/togeari-rust/{program,runtime,contracts}/`, `core/runtime/native.py` |
| 36 | [Native lanes](native/lanes.md) | `src/togeari-rust/lanes/`, `core/lanes/native.py` |
| 37 | [The GIL](native/gil.md) | `src/togeari-rust/runtime/{inbox,gate,cycle}.rs`, `core/observe/bus.py` |
| 38 | [Parity of the cores](native/parity.md) | `tests/testing/trace/test_core_parity.py` |

## Layout

```
docs/internals/
├── construct/      machine construction, knob registry, unit start, run threads
├── program/        job programs, code memory, decode tables, binding
├── runtime/
│   ├── loop/       the cycle, supervision
│   ├── pool/       jobs and records, admission, source and sink
│   ├── front-end/  fetch, order keys, run-ahead, each
│   ├── back-end/   issue, writeback, ordered / mutates
│   └── retire/     window, retire, faults, snapshots
├── lanes/          lane and pump, queues and buckets, unit health
├── policies/       admission, issue order, formers, selectors
├── plugins/        hooks, queries and commands, event bus, inspector
├── engines/        the builtin Python and web call engines
├── verification/   conformance replay, generated programs, engine checks, traces
└── native/         the Rust core: packaging and selection, dispatcher, lanes, the GIL, parity
```

## One statement, end to end

1. **Fetch.** A strand reads the instruction at its `pc`, takes the next order key and builds
   an `Inflight` whose inputs are slots and whose outputs are fresh `PENDING` slots
   ([fetch](runtime/front-end/fetch-and-keys.md)).
2. **Ready.** `check` waits on the first pending input slot; when every input is filled the
   inflight joins `Dispatcher.ready` ([issue](runtime/back-end/issue-and-writeback.md)).
3. **Issue.** The issue policy orders the ready list; `ordered` / `mutates` gates apply
   ([effects](runtime/back-end/effects.md)); the inflight runs inline, is served by the store,
   or becomes a `WorkItem` in its lane.
4. **Lane.** The lane pumps the item to a unit with free capacity ([lane](lanes/lane.md)).
5. **Writeback.** The engine's `done` posts an inbox event; `complete` fills the output slots
   and wakes their waiters.
6. **Retire.** `retire` pops the window head in order-key order, takes faults
   ([faults](runtime/retire/faults.md)) and, when the job has no live strand and an empty
   window, collects its outputs ([retire](runtime/retire/window-and-retire.md)).
