# Concepts

## A job is a sequential run

A KIR program describes one job. Its meaning is what `kohakunode`'s reference interpreter does
with it: statements run top to bottom, `branch` / `switch` / `jump` / `each` pick what runs
next, and a name holds the value last written to it. Togeari never changes that meaning. It
runs many jobs at once and, inside a job, overlaps only what the sequential run cannot tell
apart.

| Page | Covers |
|---|---|
| [The machine as a CPU](machine-as-cpu.md) | the analogy the design follows; who owns what |
| [Jobs and strands](jobs-and-strands.md) | code memory, rename, window, retire, run-ahead past branches, `each` |
| [Engines and lanes](engines-and-lanes.md) | the engine contracts, lanes, multi-issue, failover |
| [Plugins](plugins.md) | hooks at the seams, builtin plugins, queries and commands |
| [Proof](proof.md) | recording, replay and re-execution against the reference |

How to use each part is in the [guides](../guides/README.md); how each part is built is in
the [internals](../internals/README.md).
