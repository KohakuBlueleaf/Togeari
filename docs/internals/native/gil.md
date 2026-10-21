# The GIL

The dispatcher thread holds the GIL while it runs Python, and engine threads that run Python
wait for it. The native core holds it less: its own work is Rust, and it releases the GIL to
wait for events and for runs of scheduling work that touch no Python object.

## The inbox

`togeari._rs.Inbox` is the native dispatcher's inbox, with `queue.SimpleQueue`'s interface
(`put`, `get`, `get_nowait`, `empty`, `qsize`) and `drain()`; a timeout too large for the clock
is an `OverflowError` and a negative one a `ValueError`, as `SimpleQueue` gives. The cycle waits
for its first event with the GIL released, then takes every queued event at once and handles
them in order, taking again until it is empty, so events posted while it handles them are
handled in the same drain, after the ones before them.

## Regions and GIL offers

A cycle runs its scheduling work as regions when `gil_offer_us` is not `None` and no bus
listener acts on events as they come: every listener is registered `passive`
(`Bus.listen(fn, kinds, passive=True)`; the decision log and the `counters` plugin are). Any
other listener keeps the whole cycle on the GIL path, so it receives each event at the point the
Python core emits it.

A region is the same Rust code whether or not it releases the GIL. It releases it (an offer)
only when one is due: `gil_offer_us` microseconds of the core's clock passed since the last
offer or the last wait for events. A region not due runs with the GIL held and without setting
the region gate (no other thread reads the core while the GIL is held). At the end of a cycle
with an offer due, when no region of the cycle released and the cycle did not wait, the core
makes a pure offer (a release and re-take with no work in between), timed as the `offer` phase,
so threads get the GIL at the same rate with an acting listener, a Python issue policy or a
cycle with no region. Waiting for events always releases the GIL.

| `gil_offer_us` | Offers |
|---|---|
| `None` (the default) | none but the waits for events; no regions (every cycle on the GIL path) |
| `0` | at every region, and at the end of every cycle that neither ran one nor waited |
| a number > 0 | at most 1e6 / value per second on the core's clock |
| `inf` | none but the waits for events; every region runs held |

The core's clock is wall time: it runs on through the Python calls of a cycle (an engine's
`submit`, a sink flush), so an offer can come sooner than `gil_offer_us` of the dispatcher's own
work. The trace parity runs `None`, `0`, `100` and `inf`, and every other shape both at `None`
and at `0`.

The default is the measured choice of spec/11 §11.6 ([performance](../../performance/native.md)):
`None` has no shape slower than the Python core and the most faster ones. Below the dispatcher's
ceiling it waits for events often, and each wait is a release. At the ceiling it never waits, so
other threads get the GIL only at the interpreter's switch interval; `gil_offer_us` between
`20` and `300` keeps an engine thread that needs the GIL fed there, at a cost to the dispatcher
that grows as the interval shrinks ([tuning](../../guides/tuning.md#the-native-core)).

An offer costs nothing by itself. It hands the GIL to any thread waiting for it: the source
reader, the sink writer, an engine's delivery thread, a plugin worker. The dispatcher gets it
back when that thread blocks or at the interpreter's switch interval. Too many offers cut those
threads into many short runs, each paid by the dispatcher. Too few leave engine threads and
feeders waiting up to the switch interval. The Python core accepts the knob and has no GIL to
offer. The decisions are the same for every value.

| Region | Runs as Rust code | Stops for the GIL path at |
|---|---|---|
| fetch (each dirty job in order) | calls and assignments whose operands are names or scalar literals; jumps, ends, frame pops, joins, phis and waking; a branch on `True` / `False`; parking and stalling; starting `each` iterations | a container literal, a missing name or input, a branch on anything else, `switch`, `each` (its items), `ITER_END`, a step past `step_budget` |
| the issue order (at the end of the fetch region) | the order of a builtin issue policy over the ready statements | a user policy, a Python lane's fill (`LaneAware`), a hint priority Rust cannot hold (`HintPriority`) |
| retire (each dirty job in order) | retiring done statements: the window, `mutates` versions, the owner's fate, letting go of values; waking blocked statements | a fault to take, a recorded job, a positional store put, the alias check, a snapshot due, a job to finish |

A stopped pass is resumed where it stopped (the strand, its steps, the pass's budget; the
retire stage), so the order of every decision is the one the GIL-held cycle makes, and the
region goes on with the next job. Events met in a region (`stmt.fetch`, `stmt.ready`,
`stmt.retire`, `strand.*`) wait in order and are emitted, if wanted, when the region ends and
before any other Python runs; with nothing on the bus none are kept.
`Core.native_stats()` counts the regions (`regions_released`, `regions_held`), the offers
(`offers`: released regions and `pure_offers`), the steps fetched in regions and left for the
GIL path, and the issue orders and retires done in regions and left for the GIL path.

## The region gate

While a region runs, the core's gate is set; it is set and cleared with the GIL held. Every
method of `togeari._rs.Core` and `togeari._rs.Lane`, and a lane's bucket views, first wait on
the gate (a condition variable, the GIL released), so a thread other than the dispatcher's
never reads the core or a lane while the dispatcher works without the GIL. The collector's walk
over a core or a lane during a region visits only what a region never changes.

## What keeps the GIL

| Work | Why it runs with the GIL |
|---|---|
| issuing a statement | it builds the inputs dict, runs an inline function, asks a store, builds a work item and enqueues it: Python objects |
| writeback | results are Python objects; plugins and `finish` are called |
| lane forming and unit choice | the builtin selectors and the pump call the engines' `capacity()` and `load()` between the steps that choose (a unit's `capacity()` is read at its turn, after the submits to the units before it, which can change it when units share an engine); taking them before a region would change what the engines are asked and when. The steps between those calls are a few comparisons, less than a GIL release and re-take costs, and a release there under contention waits up to the switch interval for the GIL |
| timers | the timer heaps are Rust and a due timer is rare; a due timer calls an engine's `ready()` or reads and settles an item's `ext` |
| admission, queries, commands, plugins | Python by definition |
