# Tuning

The knobs, policies and lane options, what each one trades, and where its effect shows. Every
knob can change while the machine runs (`set_knob`); a lane's former and selector too
(`set_lane_policy`); the other lane options are set when the lane is built.

## Admission

Admission decides how many jobs are resident. It is consulted before each new job **while at
least one job is resident**; with no resident job the next source item is always admitted, so
a rule can never stall an empty machine. Admission also pauses while the sink queue is full.

| Rule (`togeari.policies`) | Admits while |
|---|---|
| `Fixed(n=64)` (default) | fewer than `n` jobs are resident |
| `TargetLoad(lane, waiting=8, max_jobs=4096)` | fewer than `max_jobs` resident jobs, and the lane's queue plus its units' reported `waiting` is under `waiting` |
| `MemoryBound(max_bytes)` | the estimated bytes resident jobs hold are under the cap: their inputs, the values their live slots hold, and results kept after a fault |
| `All(*rules)` | every rule admits |

Admit enough jobs to keep the slowest lane full: the requests in flight it needs divided by the
requests one job keeps in flight there. `TargetLoad` holds a lane just saturated without
counting. Watch resident jobs (`signals`), unit `waiting`, lane queue depth.

## Window

| Knob | Default | Trades |
|---|---|---|
| `window` | 64 | statements in flight per job (more overlap) against memory per job |
| `retire_buffer` | 4096 | finished statements waiting behind an older slow one |
| `run_ahead` | `True` | run past undecided branches; `False` stalls at each one |
| `step_budget` | `None` | caps instructions per job (endless loops fail instead of spinning) |

A wide `each` keeps at most a window's worth of iterations started. Watch job stats:
`window_peak`, `strands_peak`, `inflight_peak`, `parks`.

## Issue order

`issue=` orders the statements that became ready in one cycle and sets their lane priority:
`OldestJobFirst` (default; finishes jobs and frees their memory first), `Fifo`,
`DeepestFirst`, `LaneAware` (feeds the lane with the shortest queue per unit), `HintPriority`
(`@hint priority=`).

## Lanes

| Lane option | Default | Trades |
|---|---|---|
| `former` | — (batch lanes) | `SizeOrWait(size, wait_ms)`: a batch at `size` items or once an item waited `wait_ms` (or its `@hint max_wait_ms`); `Greedy()`: whatever is queued, at once; `Bucketed(inner, key)`: split batches further by `key(item)` |
| `forming` | `"late"` | `"late"`: one queue, batches formed when a unit has a free slot; `"early"`: items assigned to unit queues at enqueue (each unit forms its own batches) |
| `select` | `LeastLoaded()` | `Throughput()` (faster units take more), `RoundRobin()`, `Affinity(key="job")` (the same unit per key, for per-key caches), `ByHint()` (`@hint unit=`) |
| `max_inflight` | `None` | caps submitted items across the lane |
| `timeout_s` | `None` | per item, from submit; `@hint timeout_s` bounds a statement from issue (the earlier deadline wins) |
| `retry` | `Retry(max=3, backoff_ms=200)` | re-queues after a unit error, each after the backoff |
| `lane_dead_s` | 300 | how long queued items wait with no unit up before they fail |

A larger batch `size` trades latency for engine efficiency; `wait_ms` bounds the latency a
partial batch adds. Watch lane `batch_sizes`, queue waits, `retries`, `failovers`,
`no_unit_s`, per-unit `done` and `rate`.

A running lane's former and selector swap between dispatcher cycles:

```python
machine.command("set_lane_policy", lane="work", policy="former", value=SizeOrWait(32, 10))
machine.command("set_lane_policy", lane="work", policy="select", value="sticky")
```

A string names a policy a plugin contributed. Queued items are re-bucketed under a new former.
A refused swap (a contract method missing, a former on a call lane, none on a batch lane)
raises `ValueError` and keeps the old policy.

## Effects

`mutates` decides what a `mutates` node does while older work of its job may still fail:
`"copy-if-speculative"` (default: a copy, then run), `"wait"` (wait for the older work),
`"copy-always"`. Watch job `mutates_copies`.

## Faults

| Knob | Default | Trades |
|---|---|---|
| `squash_reuse` | 1024 | results kept per job when a fault squashes completed calls: a refetched call with the same inputs takes its result instead of running again; 0 keeps none (every refetched call runs again) |
| `squash_reuse_bytes` | 64 MiB | bytes of kept results per job: past it the oldest go first, and a single result larger than the cap is not kept; `None` sets no byte cap |

Calls with `ordered` or `mutates`, and calls bound with `store="positional"`, always run again.
Kept results count toward the job's bytes until they are used or the job ends. Watch job
`squashed` and `reused`.

## Events and recording

| Knob | Default | Trades |
|---|---|---|
| `events` | `"sampled"` | `"all"` sends every event to subscribers; `"counters"` only the always-delivered ones |
| `event_sample` | 0.1 | share of jobs with detail events while `record` is off |
| `record` | `None` | share of jobs recorded for conformance (their events are sampled too) |
| `record_limit` | 1 000 000 | recorded calls per job |

Events are built only when a plugin subscribes or listens, so a machine without event plugins
pays nothing for them. A listener receives its kinds whatever the `events` level: `Counters`
(also inside `RunReport`) listens to `stmt.fetch`, `lane.enqueue`, `lane.submit`, `lane.result`
and `job.admit`, so those events are built for every call at every level, `"counters"`
included.

## Collector

| Knob | Default | Trades |
|---|---|---|
| `gc_threshold` | `(50000, 20, 100)` | fewer collector passes over the dispatcher's many small objects against a few more MB held between passes; `None` keeps the process's own threshold |

The threshold is process-wide. While machines run, the one set last by a running machine holds;
a shorter tuple keeps the process threshold's later values. The process's own threshold comes
back when the last of them stops, or one set from outside while they ran. A first value of 0
(no automatic collection) is refused. On Python 3.14 the second value sets the incremental
collector's old-generation scan share and the third is ignored. On the ceiling shapes the
default gives 2–9% more node calls per second (medians of five interleaved rounds) than
Python's `(2000, 10, 10)` ([performance](../performance/README.md)).

## Reading a run

```python
from pathlib import Path

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.io import Iterable, Memory
from togeari.plugins import Perfetto, RunReport
from togeari.policies import TargetLoad
from togeari.testing import SimCall

lib = NodeLibrary("tune")


@lib.node(outputs=["y"])
def slow(x: int) -> int:
    return x * 2


out = Path("runs/tuning")
work = Lane([SimCall(name="sim", latency_ms=5, capacity=8)], name="work")
machine = Machine(
    program="@def (x: int)main(y):\n    (x)slow(y)\n",
    entry="main",
    libraries=[lib],
    bind={"slow": Use(work)},
    admission=TargetLoad(work, waiting=16),
    plugins=[RunReport(out / "report"), Perfetto(out / "trace.json")],
)
with machine:
    machine.run(Iterable([{"x": i} for i in range(200)]), Memory())
    machine.command("set_knob", name="window", value=128).result()
    machine.command("set_knob", name="admission", value=TargetLoad(work, waiting=32)).result()
    print(machine.inspect("lanes").result()[0]["stats"])
print(sorted(p.name for p in (out / "report").iterdir()))
```

| File | Read it for |
|---|---|
| `report/summary.json` | totals, throughput, slowest lanes, top queue waits, plugin hook time |
| `report/metrics.jsonl` | one counters snapshot per second: dispatcher busy fraction and phase times, lane queues and waits, unit busy fraction and rates, jobs (resident by state), store hits |
| `report/events.jsonl[.zst]` | every event the report received |
| `report/spec.json`, `report/env.json` | what ran: programs, bindings, lanes, unit specs, policies, knobs, versions |
| `trace.json` | open in Perfetto (ui.perfetto.dev) or `chrome://tracing`: a track per dispatcher phase, lane queue, unit (a slice per item) and job |

A dispatcher busy fraction near 1 means the dispatcher is the limit; a unit busy fraction near
1 with a growing lane queue means that lane is. `python scripts/benches/dispatcher_ceiling.py`
reports the dispatcher's own ceiling with engines that cost nothing.
