# The machine as a CPU

The machine is many threads (jobs) on one out-of-order core: a front end (the dispatcher)
that fetches, renames, issues and retires, and a back end (the engines) that does the work.

| CPU core | Togeari |
|---|---|
| thread | job |
| instruction | statement (node call, assignment, builtin) |
| architectural register | KIR name |
| physical register + renaming | value slot per write |
| reorder buffer | per-job window, retired in program order |
| functional unit class | lane (stream, batch, call) |
| one ALU in a class | one engine unit in a lane |
| multi-issue | a lane submits to every unit with free capacity |
| branch + post-dominator | `branch` / `switch` + static reconvergence point |
| precise exceptions | failures taken in program order, younger work squashed |
| I/O fence | `ordered` nodes; `mutates` waits for (or copies around) older work |
| SMT occupancy | resident jobs, set by admission |
| performance counters | the event bus and the counters plugin |

## Who owns what

| The machine | Engines |
|---|---|
| loading, checking and decoding programs | how and where work runs: threads, processes, devices, remote services |
| jobs, strands, rename, window, retire, squash | scheduling inside the engine |
| the engine contracts and the behaviour they require | resources, lifecycle details, transport |
| lanes: queues, batch forming, unit choice, multi-issue, retry and failover, timeouts, cancellation | the numbers it reports through `load()` |
| inline execution, admission, issue order, stores, sources, sinks | |
| events, counters, run reports | |

Only the dispatcher thread writes job state. Engine results enter through one inbox, so every
change to a job happens in one place, in order.

The dispatcher, the core of the machine, has two implementations of one protocol: the Python
core, the reference that defines every decision, and the Rust core, which makes the same
decisions in the same order, faster and mostly without holding the GIL. A machine runs the Rust
core when the extension is built ([native core](../internals/native/README.md)).
