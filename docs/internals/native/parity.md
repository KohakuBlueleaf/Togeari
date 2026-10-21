# Parity

Both cores take the same arguments, knobs, policies, plugins, engines, stores, sources and
sinks, and on the same events they make the same decisions in the same order.

## What both cores share

The Python core is written so a second implementation can match it exactly:

| Rule | Where |
|---|---|
| a squashed statement, a retired one and a taken fault let go of their slots, values and work item | `retire/faults.py` `squash`, `take_fault`; `retire/retire.py` |
| an ended strand lets go of its names, frames and spawner state | `runtime/state.py` `Job.end_strand` |
| jobs to fetch for are kept in the order they became dirty; phis are made in name order | `runtime/dispatcher.py`, `frontend/{control,each}.py` |
| one scheduling clock (`clock`) for the dispatcher and every lane | `runtime/dispatcher.py`, `lanes/lane.py` |
| a unit's open submissions are kept in submit order (a detach cancels them in that order) | `lanes/health.py` `UnitState.subs` |
| a lane is dead once the clock reaches the time `next_deadline` reports | `lanes/health.py` `_dead` |
| `SizeOrWait` takes a bucket its `deadline` counts as full | `policies/former.py` |
| under early forming, refused items move to a unit that admits them | `lanes/pump.py` `_respread` |
| views and commands read jobs through `JobView`'s surface; job changes go through the dispatcher | `plugin/tables.py`, `plugin/base.py`, `pool/signals.py` |

Ids (jobs, strands, submissions) differ between the cores; nothing compared depends on them:
the decision log names jobs by key and statements by region hash and offset, and builtin issue
priorities only order jobs by admission.

## The checks

`tests/testing/trace/test_core_parity.py` runs every check on both cores with the trace driver
(`togeari.testing.trace`, [traces](../verification/trace.md)) and asserts an identical decision
log (the first mismatch on failure), identical records (every field but wall-clock times), the
same cycle count and virtual time, the same dispatcher counters (jobs, calls, squashes, reuse,
waste, stores, snapshots, weak digests, machine and policy errors, records, cycles) and the same
lanes (counters, batch sizes, each unit's state, items done, errors and items in flight):

| Check | Runs |
|---|---|
| generated programs | 16 programs of the generator × 17 configurations: the default, each builtin issue order, no run-ahead, a tiny window, every `mutates` mode, no reuse, not recorded, the debug checks, a mixed admission rule, the GIL held, `Affinity` late and early, `RoundRobin` with `Greedy`; 5% item failures and an outage of one unit |
| other trace seeds | 6 programs × 2 more engine schedules × 3 configurations |
| a Python policy | an issue policy written in Python on 4 programs |
| no listener | 4 programs with nothing on the bus (no decision log): records, time and counters agree, and the Rust core ran GIL-free regions |
| lane subclasses | a subclass overriding `enqueue`, one overriding `pump` (both stay Python lanes), one adding a method (adopted, its class kept) |
| policies changed on the instance | `take` on a `SizeOrWait`, `order` on a `LeastLoaded` and on a `DeepestFirst`, `allows` on a `Fixed` |
| changes made mid-run | at the 3rd and 40th retire of 3 programs: a selector, former or forming swap, the `window` and `issue` knobs, `max_inflight`, `timeout_s`, `retry` (set and mutated), `lane_dead_s`, `unit_down`, a detach (draining and not), an attach, pause and resume, cancel, stop |
| snapshots and stores | content and positional stores and snapshots over two runs sharing a store (the second hits results and resumes jobs); the stores' keys agree |
| memory | after a run each core's machine, dispatcher and lanes are freed, and the Rust core's live jobs, strands, inflights and slots are back where they started |
| the digest | the native digest of a range of values (scalars, strings with surrogates, containers, sets of mixed members, a recursive list, pickled and unpicklable objects) is the Python digest |
| the examples | the programs of `hello`, `multi_unit` (`Throughput`, retries), `trace_replay` (stream, call and batch lanes, `TargetLoad`), `parallel` and `mixed` (`@try`, ordered nodes, a batch lane), on scripted lanes |
| coverage | the runs fault, squash and retire, and the Rust core fetches, orders and retires without the GIL and leaves some steps for it |

The whole suite also runs with `TOGEARI_CORE=rust`; its tests reach the core through the
machine, the plugins and the queries, so they check the Rust core as they check the Python one.
