# Generated programs

Seeded random KIR programs run on simulated engines under many machine configurations, and
every job is checked by [conformance](conformance.md) re-execution. Code:
`tests/machine/generated/test_generated.py`, `tests/machine/generated/test_generated_runs.py`,
`tests/conftest.py`, `src/togeari/testing/sim.py`, and `kohakunode.testing` (the generator).

## Generator

`kohakunode.testing.entry_program(seed, jobs)` returns `EntryProgram(text, entry="main",
inputs)`: helper `@def`s, then `@def (a: int, b: int = 3)main(result)`, and `jobs` input dicts
(`b` given in about half). Statements are drawn from:

| Kind | Shape |
|---|---|
| `assign`, `call` | literals, `inc`, `mix`, calls of helper `@def`s |
| `branch` | `small` then `branch` over two arms |
| `switch` | `pick` then a three-way `switch` with a default |
| `parallel` | two arms |
| `each` | an `each` appending to a list |
| `loop` | a `jump` loop with an exit `branch` |
| `try` | `@try` around `flaky` (fails when `x % 4 == 1`) |
| `dataflow` | a `@dataflow:` block written out of order |
| `ordered` | `emit`, which appends to `EFFECTS` |
| `mutate` | `bump` (mutates `box`) between reads of the box through an alias |
| `each_exit` | an `each` whose body can `jump` out |
| `try_each` | `@try` around an `each` whose body calls `flaky` |

Names are read only where every path wrote them, and loops are bounded.

## Machine under test

`conftest.build(program, entry, n_units, seed, latency_ms, record=1.0, inline_std=True,
programs=None, forming="late", select=None, store_mode=None, item_error_rate=0.0, outage=None,
retry=None, **knobs)`:

| Lane | Units (`CountOutage`) | Capacity | Nodes |
|---|---|---|---|
| `call` | `n_units` × call | 8 | `inc`, `small`, `emit`, `bump`, helper `f*` |
| `batch` | `n_units` × batch, `SizeOrWait(8, 1)` | 2 | `mix`, `pick` |
| `stream` | `n_units` × stream | 256 | `flaky`, `total` |
| inline | — | — | `kir.std.*` |

- `select`: one selector name for every lane, or one per lane, from `SELECTORS`
  (`least_loaded`, `throughput`, `round_robin`, `affinity`, `by_hint`).
- `store_mode`: `content` or `positional` on `STORABLE` (generator nodes that are neither
  `ordered` nor `mutates`).
- `outage=(down_at, polls)`: unit `i` of each lane but the last goes down at its
  `down_at + i`-th submit and comes back after `polls` health polls.
- Admission is `Fixed(32)`; every job is recorded.

`CountOutage` (a `SimEngine`) is down from its `down_at`-th submit, or after `go_down()`, until
its `ready()` has been asked `polls` times; while down, `submit` answers `UnitError(SimOutage)`.

`pure(name)` is true for every node not in `IMPURE` (the `ordered` and `mutates` nodes of the
generator's and the standard library): those are re-executed, the rest replayed.

## Simulated engines

`SimEngine` (`SimCall`, `SimBatch`, `SimStream`) runs the node's `PythonAdapter` payload on its
own timer thread after a delay:

| Parameter | Effect |
|---|---|
| `latency_ms`, `tail` | delay `latency * (1 + tail * h^4 * 20) * (0.5 + U)`, `h` from the item's digest: heavy-tailed, input-dependent; `latency_ms=0` is no delay and `tail=0` skips the digest |
| `seed` | two seeded generators: delays (submitting thread), injected failures (timer thread) |
| `capacity` | concurrent items (call, stream) or batches (batch); defaults 8, 2, 256 |
| `max_batch` | 16 |
| `item_error_rate` | injected `ItemError(SimError)` |
| `outages` | `[(start_s, end_s)]` after `start()`: `submit` answers `UnitError(SimOutage)` |
| `speed`, `samples_ms` | scale the delay; draw delays from recorded samples instead |

`stop()` answers every pending ticket with `UnitError(EngineStopped)`; a `submit` after
`stop()` answers its items the same way at once and returns `-1`. Counters and in-flight counts
change under the engine's lock.

`cancel` drops a pending timer and answers `Cancelled()` for its items; a dropped timer stays in
the heap and is skipped when due. `stop` stops the clock and answers every pending ticket with
`UnitError(EngineStopped)`, so a stopped unit leaves no item unanswered.

## Checks

`test_generated(seed, config)`: 32 jobs of one generated program per seed (`TOGEARI_SEEDS`,
default 20) under each configuration, run within `bounded(..., 120 s)`. Every key must come back
and every job must conform with `mode="reexecute"`, `pure=pure`, and `engine_errors` set only for
the fault configurations.

| Config | Units | Knobs and options |
|---|---|---|
| `sequential` | 1 | `window=1`, latency 0.2 ms |
| `w64` | 3 | `window=64` |
| `w256` | 3 | `window=256` |
| `faults` | 3 | `window=64`, `FAULTS` |
| `early` | 3 | `window=64`, `forming="early"` |
| `early_faults` | 3 | `window=256`, `forming="early"`, `FAULTS` |
| `selectors` | 3 | `window=64`, one selector per lane, rotating with the seed |
| `no_runahead` | 2 | `window=16`, `run_ahead=False` |
| `copy_always` | 2 | `window=64`, `mutates="copy-always"` |
| `mutates_wait` | 2 | `window=64`, `mutates="wait"` |
| `content_store` | 2 | `window=64`, `store_mode="content"` over a `MemoryStore` |
| `plugged` | 3 | `window=64`, latency 1 ms, a `PassThrough` plugin on every hook and on `events="all"` |

`FAULTS` is `item_error_rate=0.01`, `outage=(2, 1)`, `retry=Retry(max=5, backoff_ms=1)`. The
fault configurations also require failovers; `content_store` requires store lookups; `plugged`
requires `pre_admit`, `on_job_start`, `on_job_end`, `pre_submit`, `post_result` and `pre_sink`
to have been called, and `on_job_end` once per job.

`test_generated_runs.py`, per seed:

| Test | Checks |
|---|---|
| `test_programs_mixed_in_one_machine_and_one_loaded_mid_run` | four generated programs with renamed entries; three loaded at construction, the fourth through `machine.load` halfway through the source; jobs interleaved across entries; every job conforms against its own entry |
| `test_snapshots_resume_and_conform` | `snapshot_every=1`, `window=4`, no run-ahead; a first machine is stopped once a snapshot exists; a second machine over the same store resumes jobs (`stats["resumed"]`), every job conforms, and no snapshot is left once every job ended ok |
| `test_positional_store_rerun_conforms` | the same jobs on two machines over one store with `store_mode="positional"`: no hit in the first run, hits in the second, every job conforms |
| `test_ordered_effects_follow_program_order` | `EFFECTS` written by `emit` equals the order of `emit` calls in the job's record |

## Hangs and timeouts

- `conftest.bounded(machine, fn, seconds)` runs `fn` on a daemon thread; past `seconds` the
  test fails with "hang" and `dump(machine)`: resident jobs, the detail of the first four, and
  the lanes (`tests/machine/smoke/test_hang_detector.py` checks it on an engine that never
  answers).
- The per-test watchdog (`pytest_runtest_call` in `tests/conftest.py`): a test past its bound
  (`@pytest.mark.timeout(seconds)`, else `TOGEARI_TEST_TIMEOUT`, default 900 s) has every
  thread's stack written to `runs/tests/watchdog.txt` and ends the session
  (`faulthandler.dump_traceback_later(..., exit=True)`).

## Scaling

`tests/scaling/test_scaling.py` uses `scripts/benches/scaling.py`: per-job cost with N jobs
resident at once (N = 2000) and per-item cost of one `each` over N items (N = 1000), each the
minimum of three runs on a zero-latency `SimCall` lane. The cost at 4N must be at most
`FACTOR = 2.0` times the cost at N, so a cost that grows with the queue, the pool or the strand
count fails.
