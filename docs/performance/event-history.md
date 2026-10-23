# Event-history retention and requested-checkpoint cost

## Reproduce

```bash
uv pip install --python .venv/Scripts/python.exe --target runs/bench-deps psutil==7.2.2
# PowerShell:
$env:PYTHONPATH=(Resolve-Path runs/bench-deps).Path
.venv/Scripts/python.exe scripts/benches/event_history.py --out event-history.json
```

On POSIX set `PYTHONPATH=runs/bench-deps` and use `.venv/bin/python`. The default
CPU-only preset runs both cores, 128/512/2048 events, 256/4096-byte strings, and
two fresh-process repetitions per point. Raw payload is capped at 8 MiB per
sample; larger runs require `--large`. `--record all` enables ordinary call
recording; the reported run uses the default **`record=None`**. This is a local
measurement, not a correctness-CI timing threshold.

Raw samples and medians: [event-history-20261023.json](data/event-history-20261023.json).
The report records exact checkout revisions, extension paths, package versions,
platform, knobs, each repetition, and memory/timing coverage. Tested pair:
KIR `a9b2a647379bf217cebf8e9a42375bc262fbd9df`, Togeari
`661e5a681e1fa8eeb50d21436349c55c9d0c6818` plus the benchmark script in this commit.
Windows 11, CPython 3.13.14, psutil 7.2.2; both native extensions rebuilt locally.

## Method and coverage

The job emits its finite prefix and remains resident at a controlled host-call
gate. A nonretaining sink discards payloads; a SQLite WAL/FULL journal retains
commitments on disk. Measurement begins after delivery acknowledgments and
outbox reservations drain: **all 24 samples had zero pending events**. A disk
checkpoint store measures its write and fsync separately. The dependent suffix
stays unresolved throughout capture, and the original job is then cancelled.

- **History MiB:** deduplicated reachable Python object sizes of the resident
  committed-event list. This includes envelope/origin/container overhead, not
  just payload. Different origin representations/object sharing explain some
  per-core differences; this is not total resident-job or native memory.
- **RSS:** whole-process memory includes native allocations, SQLite, runtime
  state and allocator retention. Raw samples include warm baseline, resident
  RSS and their delta; observed deltas ranged from 585,728 to 25,927,680 bytes.
  They are not a precise attribution to event history. `tracemalloc` numbers
  cover Python allocations only and are also recorded.
- **Encode:** wall time inside the existing synchronous snapshot codec,
  instrumented locally before Machine construction (the native Host captures
  the same callable). It includes validation and serialization, not arbitrary
  earlier cut planning. No production instrumentation/API was added.
- **Pack:** the synchronous checkpoint-manager capture callback, including
  artifact validation/packing and queueing. It is separate from codec time.
- **Independent delay:** a tiny independent job is enqueued immediately before
  encoding, then an observer records its completion. This includes synchronous
  capture plus scheduling/observer overhead, not just codec CPU time. It is
  independent of the deliberately unresolved suffix and storage acknowledgment.
- **Store:** the store's actual write+fsync. Total request-to-persisted time also
  includes cut drain, manager packing, worker queueing and dispatcher completion.
  `post_encode_to_persisted_ms` in the raw data is **not** pure storage latency.

Allocation tracing was enabled during these samples and adds overhead. These
are instrumentation-on cost characterizations, not uninstrumented throughput
claims. Two repetitions describe an initial trend, not a confidence interval.
The quick preset uses one fixed input string per job, captured separately per
emission; checkpoint artifacts also contain the finite iteration/captured values.

## Measured medians

MiB means 1,048,576 bytes. All times are milliseconds; individual observations
are retained in the linked JSON.

| Core | Events | Payload B | History MiB | Artifact MiB | Encode | Pack | Independent delay | Store | Total checkpoint |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Python | 128 | 256 | 0.27 | 0.09 | 38.89 | 4.67 | 44.98 | 2.83 | 51.78 |
| Python | 512 | 256 | 1.08 | 0.34 | 132.45 | 11.30 | 144.87 | 2.14 | 151.95 |
| Python | 2048 | 256 | 4.34 | 1.36 | 516.76 | 49.69 | 568.63 | 3.75 | 575.79 |
| Python | 128 | 4096 | 0.74 | 0.56 | 35.42 | 3.52 | 40.48 | 3.10 | 48.71 |
| Python | 512 | 4096 | 2.96 | 2.22 | 143.01 | 14.17 | 158.56 | 2.99 | 167.46 |
| Python | 2048 | 4096 | 11.84 | 8.87 | 559.39 | 55.37 | 617.14 | 8.71 | 631.26 |
| Rust | 128 | 256 | 0.18 | 0.08 | 20.29 | 2.10 | 22.98 | 2.50 | 30.93 |
| Rust | 512 | 256 | 0.71 | 0.29 | 85.08 | 7.40 | 93.33 | 2.25 | 98.91 |
| Rust | 2048 | 256 | 2.84 | 1.16 | 310.43 | 29.85 | 341.65 | 4.06 | 349.96 |
| Rust | 128 | 4096 | 0.65 | 0.55 | 22.84 | 2.68 | 26.21 | 2.90 | 34.57 |
| Rust | 512 | 4096 | 2.58 | 2.17 | 95.21 | 9.19 | 105.27 | 3.88 | 112.86 |
| Rust | 2048 | 4096 | 10.34 | 8.66 | 394.92 | 39.43 | 435.80 | 7.14 | 448.58 |

## Operational conclusion and stopping point

Retained history and artifact size grow approximately linearly in this preset,
**even with ordinary call recording disabled and delivery backlog drained**.
Outbox count/byte limits bound pending delivery, not the full historical output
stored in `job.stats["committed_events"]["events"]`. Call-recording limits do not
bound that history either.

The synchronous codec and artifact packing dominate these measured checkpoint
latencies; independent jobs experience the corresponding pause. Off-dispatcher
storage does **not** make capture serialization nonblocking. Budget payload and
history sizes and checkpoint frequency accordingly; prefer smaller logical jobs
when a bounded total history is operationally necessary.

No runtime optimization or persisted-format change was justified in this round.
The measured linear cost is a documented current limit, not claimed as a newly
fixed leak. Externalized history, partial collector serialization, a new outcome
log, and distributed/exactly-once delivery remain separate design work.
See [committed events](../guides/committed-events.md) and
[requested checkpoints](../guides/checkpoints.md) for the preserved contracts.
