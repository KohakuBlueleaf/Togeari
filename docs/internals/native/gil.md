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

## GIL-free regions

A cycle releases the GIL for its regions when the knob `release_gil` is on (default `True`; the
Python core accepts it and has no GIL to release) and no bus listener acts on events as they
come: every listener is registered `passive` (`Bus.listen(fn, kinds, passive=True)`; the
decision log and the `counters` plugin are). Any other listener keeps the whole cycle under the
GIL, so it receives each event at the point the Python core emits it.

| Region | Runs without the GIL | Stops for the GIL at |
|---|---|---|
| fetch (each dirty job in order) | calls and assignments whose operands are names or scalar literals; jumps, ends, frame pops, joins, phis and waking; a branch on `True` / `False`; parking and stalling; starting `each` iterations | a container literal, a missing name or input, a branch on anything else, `switch`, `each` (its items), `ITER_END`, a step past `step_budget` |
| the issue order (at the end of the fetch region) | the order of a builtin issue policy over the ready statements | a user policy, a Python lane's fill (`LaneAware`), a hint priority Rust cannot hold (`HintPriority`) |
| retire (each dirty job in order) | retiring done statements: the window, `mutates` versions, the owner's fate, letting go of values; waking blocked statements | a fault to take, a recorded job, a positional store put, the alias check, a snapshot due, a job to finish |

A stopped pass is resumed where it stopped (the strand, its steps, the pass's budget; the
retire stage), so the order of every decision is the one the GIL-held cycle makes, and the
region goes on with the next job. Events met in a region (`stmt.fetch`, `stmt.ready`,
`stmt.retire`, `strand.*`) wait in order and are emitted, if wanted, when the region ends and
before any other Python runs; with nothing on the bus none are kept.
`Core.native_stats()` counts the regions, the steps fetched in them and left for the GIL, and
the issue orders and retires done in them and left for the GIL.

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
