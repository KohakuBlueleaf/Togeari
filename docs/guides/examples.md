# Examples

Each example is a machine module under `examples/`: a `.kir` program, its nodes, and a
`machine.py` whose `build()` returns `(machine, source, sink)`. Run one with
`python examples/<name>/machine.py` (prints its own report), or with
`togeari run examples/<name>/machine.py` (prints the summary), `togeari check ...` (build and
check only), `togeari units ...` (engine checks, where the module gives samples).

Every example records every job, checks each one against the reference interpreter
(`conformance: N/N jobs match the reference`) and exits nonzero when a job fails or does not
match. `tests/examples` runs them that way: the CPU examples always, `mixed` (12 stations,
`python examples/mixed/machine.py 12`) under the `gpu` marker.

| Example | Engines | Shows |
|---|---|---|
| `hello` | `SimCall` | `each`, `switch`, `@try`, a slow service, conformance |
| `multi_unit` | three `SimCall` units | one lane, units of different speed, `Throughput`, an outage and failover |
| `trace_replay` | `SimStream`, `SimCall`, `SimBatch` | the three lane kinds side by side, batch forming, `TargetLoad` |
| `parallel` | `PythonCallEngine` (threads) | one job's independent work running at once: `parallel` arms, run-ahead past a branch, an `each` fan-out |
| `records` | `PythonCallEngine` (threads) | typed immutable records, visible field operations, codec boundaries, replay and re-execution |
| `mixed` | `WebCallEngine`, `PythonCallEngine` (processes and threads), the builtin batched device engine | three kinds of engine behind one program, `ordered` nodes, the run report and recorder, `togeari conform` |

## hello

`hello.kir` makes one greeting card per language. Unknown languages ask a slow translation
service (a `SimCall` unit, 50 ms, capacity 4); a failure inside `@try` becomes a `?` card. The
script checks every job against the reference interpreter:

```
Ayu: 3 cards ['Hello, Ayu!', 'Konnichiwa, Ayu!', 'Bonjour, Ayu!']
...
Dev: 3 cards ['Bonjour, Dev!', '?, Dev!', 'Hello, Dev!']
Eli: 0 cards []
conformance: 5/5 jobs match the reference
```

## multi_unit

`squares` sums the squares of a list on a `work` lane of three units (speed 1, 2, 4) chosen by
`Throughput`; the fastest unit is down from 0.3 s to 0.8 s after start. The script checks
every job's sum against a plain Python sum and the reference interpreter, and prints the run
time with the keys of any wrong results (`none` when every job is right), the lane's retries,
failovers and recoveries, and how many items each unit took: the faster units take more, and
the outage costs one retry, no job error.

## trace_replay

`review` fetches a document (stream lane), splits it, parses every part (call lane, four
workers) and scores the parts (batch lane, `SizeOrWait(32, wait_ms=10)`), then keeps the
document when its best score passes a bar. The simulated units replay latency samples from
`latencies.json`; admission is `TargetLoad(fetcher, waiting=24)`. The script prints the run time
against the time the same calls take one after another, the batch sizes the lane formed, and
the conformance of every job.

## parallel

`report` is one city report written as a sequential program: two `parallel` arms, an
independent lookup, a branch on the weather, and one survey per district. Part 1 runs one job
and draws when each call ran: the arms, the lookup and every survey start together, and only the
storm alert waits for the branch. The timeline starts once the machine and its workers are up.
It prints the job's `strands_peak` and `inflight_peak`, and the job's time against the same
calls one after another. The strands peak is lower than the calls in flight: a strand ends once
it has fetched its code, while its calls stay in flight. Part 2 runs four cities as four jobs.

## records

`records.kir` constructs a point, sends it to a simulated slow worker operation, and projects
both the original and translated coordinates. The script checks the original value is
unchanged, verifies every job by replay/re-execution, and round-trips the returned record
through explicit tagged JSON. It needs no device or external service. See
[Typed records](records.md) for schema and adapter APIs.

## mixed

`survey` looks up a station's gain on a local HTTP registry (`WebCallEngine` with `JsonHttp`;
an unknown station keeps gain 1.0 through `@try`), computes features in worker processes
(`PythonCallEngine`, `mode="process"`), scores them in batches on the builtin batched device
engine, classifies inline, and writes an ordered log from worker threads. It needs the optional
extras for the web and device engines and a device. The run report, the recorded jobs and the
log go to `runs/mixed/`; afterwards

```
togeari conform runs/mixed/report
togeari conform runs/mixed/report --reexecute features --reexecute classify --reexecute majority
```

replay every job (with `examples/mixed` on `PYTHONPATH`), the second also running those nodes
again ([conformance](conformance.md)).
