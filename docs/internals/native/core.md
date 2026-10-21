# Dispatcher core

`togeari._rs.Core` is the dispatcher of a Rust machine: the job pool, fetch and rename, issue,
writeback, faults, retire, admission and the cycle, each a port of its Python module with the
same order of operations. `togeari.core.runtime.native.NativeDispatcher` subclasses it with what
stays Python: the thread loop and its supervision (`loop`, `died`, `abandon`, `refuse_inbox`),
query and command answers, the run's source buffer and sink queue (`attach_run`,
`take_source`, `to_sink`, `flush_sink`), the collector ask, and the lane hooks other threads
call (`lane_done`, `lane_woke`). The base class has no `__dict__`; the subclass's own holds its
attributes.

## State

Scheduling state is Rust: jobs, strands, frames, joins, `each` spawners, inflights, windows,
slots and waiters, order keys, the ready list, the dirty set, blocked statements, counters.
Objects hold each other as the Python core's objects do: `Rc<RefCell<…>>` where the Python
core holds a reference, `Weak` for the back references to a job (strand, inflight). A slot
therefore frees when the Python core's does, and its held bytes leave the job then.

| Python core | Rust core |
|---|---|
| `Slot` (value, state, waiters, forward, phi, muts) | `state/slot.rs` `Slot`; `HeldSlot.__del__` is `Slot`'s `Drop` |
| `NameMap` / `Names` (shared base, delta, fold at 32) | `state/names.rs`, `IndexMap` in Python dict order |
| `Frame`, `Join`, `EachSpawn` | `state/frame.rs`; frames shared as `Rc<Vec<Rc<Frame>>>` |
| `Strand`, `Inflight`, `Window`, `Job` | `state/{strand,inflight,window,job}.rs` |
| order keys (tuples of ints) | `state/key.rs` `Key(Rc<[i64]>)`, compared as tuples |
| `Job.key`, the running keys | the key object; a Python `set` of keys (Python equality) |
| `Job.stats` | `JobStats`: the fixed counters, `program`, then keys set later, in Python's order |
| `Dispatcher.stats` | `Stats`, read as the Python core's dict (`stats` getter) |

No field is borrowed across a call into Python, nor while a view builds Python objects, so a
plugin, a listener, a selector, a finalizer or a view may read the core from inside any call.

## Memory

A strand that ends lets go of its names, frames and spawner state (`Job.end_strand` in both
cores), and a taken fault lets go of its values and slots as a retired statement does
(`take_fault` in both cores): the slots a strand waited on name it as a waiter, and the strand
holding those slots would otherwise keep both alive. A job that ends lets go of every statement
of its window. `togeari._rs.live_objects()` counts the live jobs, strands, inflights and slots
of every core in the process; after a run they are back where they started.

`Core`, `Lane`, `Done`, `Bucket`, `Owner` and `Inbox` show the collector every Python object
they hold. The core's walk (`runtime/traverse.rs`) goes through its host, code, entries, lanes,
jobs, the ready and blocked statements and the pending events, visiting each shared Rust object
once; a `RefCell` already borrowed is skipped (a reference the collector does not see keeps an
object alive, one it sees twice could free it). `__clear__` drops the job state, code and
caches, and the job graph's Rust cycles with them. A dropped machine is freed by the collector
on either core.

## Values

A value is a Python object held as `Val = Rc<Py<PyAny>>`: copying a slot's value (a name
read twice, a literal, an `each` item) shares the `Rc` and never touches the object's
reference count. Inputs and `mutates` versions name their ports by index into the
instruction, so building an inflight needs no Python object either. Values reach Python where
the Python core hands them over: the inputs dict at issue, inline calls, work items, results,
records, snapshots and stores.

## Digests

`contracts/digest.rs` computes the canonical digest of `togeari.core.contracts.digest` byte for
byte: `None`, `bool`, `int`, `float`, `complex`, `str`, `bytes`, `bytearray` and the builtin
`list`, `tuple`, `dict`, `set` and `frozenset` (dict keys and set members in the digest's
order, a container met again inside itself weak) are fed natively into a native SHA-256. Every
other value, a subclass of a builtin type, a string with lone surrogates, and a container type
a digest is registered for are fed by the Python digest's own code (`feed_bytes`), whose bytes
go into the same hash. The core digests inputs (records, fingerprints, squash reuse, positional
keys), content store keys and engine identities this way; `togeari._rs.digest` and
`digest_with_weak` expose it, and the parity checks compare it with the Python digest.

## Code memory

The core imports a region once, when a job first starts in it (`program/python.rs`): every
instruction with its operands, binding and hint (with `int(hint priority)` when it is an int),
and for each branch, switch and `each` its reconvergence point, whether it is structured, its
may-write set and per-address write tables, and for an `each` its body tables and carried
names. Names are interned as integers; name sets carry the `ALL` marker as a flag; phis are
made in the names' text order, as the Python core sorts them. Regions loaded while the machine
runs are imported on their first job.

## The stages

| Stage | Rust | Callbacks into Python |
|---|---|---|
| fetch, rename, run-ahead, `each` | `frontend/{fetch,control,each}.rs` | container literals (`fresh`), the items of an `each`, switch cases, fault messages |
| readiness, issue | `backend/issue.rs` | the inputs dict, inline functions, stores, work items, a user issue policy |
| writeback | `backend/writeback.rs` | `post_result` / `pre_submit` plugins, `finish`, value sizes beyond `str` / `bytes` / numbers |
| faults, squash, reuse | `retire/faults.rs` | sizes of kept results, `on_fault` |
| retire, finish | `retire/retire.rs` | records, positional stores, snapshots, the alias checks |
| snapshots | `retire/snapshot.rs` | pickle (the Python core's layout: either core reads the other's snapshots) |
| jobs and records | `pool/jobs.rs` | `pre_admit`, `pre_sink`, `on_job_start`, `on_job_end`, `JobRecord`, handles |
| admission | `pool/admission.rs` | lane signals and engine loads, a user rule, `vote_admit` |
| the cycle | `cycle.rs` | queries, commands and posted calls, the collector ask |

Knobs are read from the machine's knob dict through interned keys, when the Python core reads
them; a count knob compares with a count as Python compares an int with an int or a float.

## Builtin policies

`runtime/builtins.rs` knows the builtin policy methods by their function objects: a method of a
policy is builtin when it resolves (on the policy's class) to one of them and the instance does
not set it on itself. The core then runs that method natively; any other method is called back
through the same `ReadyView`, `PoolView` and `Signals` objects the Python core passes.

| Policy method | Native |
|---|---|
| issue `order` of `Fifo`, `OldestJobFirst`, `DeepestFirst`, `LaneAware`, `HintPriority` | the order; `LaneAware` reads native lanes' fills, a Python lane's by its `queued()` and `units_up()`; a hint priority Rust cannot hold makes `HintPriority` call its own `order` |
| issue `priority` of the same | the priority; `HintPriority` takes `int(hint)` |
| admission `allows` of `Fixed`, `TargetLoad`, `MemoryBound`, `All` | the rule, its settings (`n`, `lane`, `waiting`, `max_jobs`, `max_bytes`, `rules`) read at each check |

The issue knob is read again before each statement's priority, so a policy swapped meanwhile
applies from the next statement, as in the Python core.

## Events

The core builds an event only when the bus wants it: with nothing listening or subscribed it
reads three containers and builds nothing. Fields and their order are the Python core's; the
decision log of [parity](parity.md) is a passive bus listener and reads the same events from
both cores ([the GIL](gil.md) has when events of a region are emitted).

## Work items and their owners

A work item's `owner` is a `togeari._rs.Owner`: the inflight's id, the job (`job`, a `JobView`;
`job_id`), the instruction, the order key, the entry name and the lane name, which lane events
and `pre_submit` read. The core finds the inflight by id while it is held; once it is squashed
or retired the owner keeps its `fate`, set without a Python call, and a late result counts as
`wasted` exactly where the Python core counts it. `post_result` receives the result object the
engine gave.

## Views

`NativeDispatcher.jobs` maps job ids to `JobView`s that read the core live: `key`, `entry`,
`inputs`, `paused`, `issued`, `unfinished`, `runnable`, `stats`, `held`, `window`
(`InfView`: `instr`, `key`, `state`, `item`), `strands` (`StrandView`: `id`, `state`, `pc`,
`spawn`, `next_key()`) and `slot_counts()`. `job_state` (plugin base), the `jobs` / `job`
queries and the job commands read jobs through this surface on both cores; commands that
change a job go through the dispatcher (`pause_job`, `resume_job`, `cancel_job`), and the
counters through `job_count()`, `window_total()` and `inline_totals()`.
