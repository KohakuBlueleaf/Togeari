# Performance

Measured on one desktop machine with one GPU (`cuda:0`, allocator capped per process). One run
per configuration on the release tree. The raw numbers of the engine study are in
`runs/s1/full/` (`m1.json` … `m5.json`, per-configuration logs) after
`python scripts/studies/s1/run.py --scale full`.

## Dispatcher

`scripts/benches/dispatcher_ceiling.py`: simulated engines that answer at once, so the
dispatcher is the only cost. Five rounds; each round runs every shape with the default
`gc_threshold` and with the process's own threshold (`None`, Python's `(2000, 10, 10)`), one
after the other. Node calls/s, median (best–worst) of the five; jobs/s and busy of the median
run.

| workload | default knob | process threshold | gain | jobs/s | dispatcher busy |
|---|---|---|---|---|---|
| a chain of inline nodes | 78,989 (83,845–68,877) | 69,551 (72,605–68,809) | +14% | 3,950 | 0.99 |
| a chain of calls on an engine lane | 28,702 (29,394–26,982) | 26,872 (28,568–26,585) | +7% | 1,435 | 0.81 |
| `each` with batched calls | 39,729 (41,277–37,239) | 37,393 (38,716–34,963) | +6% | 393 | 0.92 |

![Dispatcher ceiling per workload](../assets/dispatcher-ceiling.svg)

Retired statements release their work items and payloads, so few objects stay alive between
collector passes and the process threshold costs little more than the default.

`scripts/benches/scaling.py` (the minimum of three runs; `tests/scaling` fails when the
cost at 4N is above twice the cost at N):

| resident jobs | 500 | 2,000 | 8,000 |
|---|---|---|---|
| µs per job | 54.2 | 55.8 | 56.4 |

| items in one `each` | 250 | 1,000 | 4,000 |
|---|---|---|---|
| µs per item | 77.9 | 78.4 | 82.2 |

`tests/scaling` also checks loop iterations: a `jump` loop and a `mutates` loop cost the same
per iteration at 500 and 2,000 iterations (66–87 µs per iteration up to 8,000).

## Engines (study S1)

Each phase is a 1 s warm-up and a 2.5 s measured window. "direct" feeds the engine
from a plain thread; "lane" goes through a machine; "+ ceiling" runs a chain workload at
the dispatcher's ceiling next to it; "+ paced" runs it at 100 jobs/s.

### Handoff per transport (one-way p50)

| bytes | thread queue | pipe + pickle | shared memory, copy | shared memory, mapped | HTTP localhost |
|---|---|---|---|---|---|
| 1 KB | 3 µs | 183 µs | 315 µs | 59 µs | 292 µs |
| 100 KB | 2 µs | 115 µs | 477 µs | 295 µs | 381 µs |
| 2 MB | 8 µs | 4.8 ms | 2.3 ms | 1.0 ms | 2.8 ms |
| 8 MB | 2 µs | 18.2 ms | 7.5 ms | 3.4 ms | 9.9 ms |

Buffers of at least 1 MiB cross through shared memory by default
(`SHM_THRESHOLD`, `togeari.engines.common.transport`).

### A network next to the dispatcher

Items/s of a 64 × 1024 network (batch 64) through a lane with a 2 ms batch wait; the
dispatcher alone peaks at 21,995 calls/s with busy 0.86.

| isolation | kernels | lane alone | lane + 100 jobs/s | lane + ceiling |
|---|---|---|---|---|
| thread | eager | 5,220 (busy 0.36) | 4,050 (busy 0.40) | 229 |
| thread | CUDA graph | 11,896 (busy 0.75) | 9,641 (busy 0.87) | 2,079 |
| process | eager | 6,390 (busy 0.39) | 5,609 (busy 0.63) | 523 |
| process | CUDA graph | 9,583 (busy 0.66) | 7,160 (busy 0.74) | 485 |

Fed directly (no machine), the same network runs 8,313 items/s eager and 50,906 with CUDA
graphs in a thread; 7,370 and 23,616 in a process.

### Two models on one device

Items/s with both models running, divided by items/s alone:

| layout | model a | model b | device MB |
|---|---|---|---|
| thread, `co_resident` | 0.50 | 0.47 | 1,979 |
| thread, `swappable` | 0.17 | 0.11 | 1,730 |
| thread, `priority_streams` | 0.53 | 0.95 | 1,739 |
| thread, two engines | 0.21 | 0.94 | 1,681 |
| process, `co_resident` | 0.45 | 0.60 | 1,643 |
| process, two engines | 0.81 | 0.77 | 2,116 |

### Python work

8 workers, items/s fed directly:

| work (one call) | thread | process + pickle | process + shared memory |
|---|---|---|---|
| pure-Python CPU loop (2.2 ms) | 364 | 2,748 | 2,781 |
| GIL-releasing hash (1.8 ms) | 4,035 | 3,380 | 3,368 |
| small call on a large buffer (0.06 ms) | 9,496 | 378 | 828 |

### HTTP client

`WebCallEngine`, 64 requests in flight, 1 KB bodies, a local service in its own process:

| target rate | through a lane | direct | client CPU per request |
|---|---|---|---|
| 100 / s | 96.7 / s | 96.6 / s | 1.35–2.07 ms |
| 500 / s | 500 / s | 502 / s | 0.71–1.30 ms |
| 2,000 / s | 1,159 / s | 1,503 / s | 0.65–0.83 ms |
| unlimited | 1,205 / s | 1,581 / s | 0.61–0.82 ms |

## Builtin defaults from these numbers

| engine | default | because |
|---|---|---|
| `PythonCallEngine` | `mode="thread"` | threads win for GIL-releasing and small calls; use `mode="process"` for Python-heavy CPU work (7.5×) |
| `PythonCallEngine` | `transport="shm"`, shared memory from 1 MiB | a pipe is faster up to ~100 KB, shared memory from ~2 MB |
| `TorchEngine` | `schedule="priority_streams"` | the best sharing at the same device memory as `co_resident` |
| `TorchEngine` | `isolation="thread"` | the highest throughput with the dispatcher under half busy; `process` for crash isolation or an idle dispatcher |
| `WebCallEngine` | `pool_size=2` | 0.62–0.66 ms of client CPU per request at 1 or 2 pools, more above |

## Machine knobs (P6 studies)

`python scripts/studies/p6/run.py --study A|B|C --scale full` writes every configuration and a
`summary.md` per study to `runs/p6/studies/full/`.

**Event level and observers** (node calls/s, median of 3 rounds):

| shape | no plugin, `"all"` | `RunReport`, `"all"` | `RunReport`, `"sampled"` | `RunReport`, `"counters"` |
|---|---|---|---|---|
| chain of inline nodes | 77,680 | 25,892 | 54,931 | 62,236 |
| chain of calls | 29,576 | 10,139 | 16,280 | 22,374 |
| `each` with batched calls | 41,586 | 15,775 | 24,318 | 29,705 |

Events cost only when a plugin receives them; a `RunReport` with `"all"` and full recording
(`record=1.0`) are the expensive settings. `Counters` costs 10–20%.

**Knob defaults.**
- `window` (16 / 256), `retire_buffer` (1024) and `event_sample` (0 / 1) stay within ±7% of
  the defaults on every workload.
- `Fixed(16)` admission costs 29–47% on lane-bound workloads.
- `Fixed(256)` gains at most 7% for 4× the pool bytes.
- Inline nodes run at a p50 of 0.5–5 µs, against `inline_warn_us=50`.

**Device batches** (`examples/mixed`, 480 stations): the mean batch follows `SizeOrWait`'s
wait (about 5 at 2 ms, 8 at 5 ms, 11–13 at 10 ms, 18–24 at 20 ms), admission from `Fixed(32)`
to `Fixed(256)` barely moves it, and the run time stays at 5.2–6.4 s.

**`squash_reuse`** (`examples/mixed`, 480 stations, record off, one run each): with reuse on,
46 statements took a kept result instead of being sent again, and the `score` and `cpu`
lanes each received 36 fewer submits than with reuse off (3,831 against 3,867, and 3,835
against 3,869). The two counts measure different things: `reused` counts refetched
statements served from the job's kept results, over every lane; a lane's submits also move
with batching and with how far each job ran ahead before its fault, which differs between the
two runs. Run time is within noise of reuse off.

## A full run: `examples/mixed`

120 jobs over an HTTP registry, 8 worker processes and a network on `cuda:0`, every job
recorded, one run: 5,634 node calls in 4.07 s (29 jobs/s), 12 faults taken inside `@try`;
batches of the network 1–11 (1,039 items in 175 batches, mean 5.9); 120 / 120 jobs conform
in replay and in re-execution; the ordered survey log is in program order for every
station.
