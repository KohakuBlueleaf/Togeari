# Decision logs and traces

A decision log lists every scheduling decision of one machine, in the order the dispatcher made
it. The trace driver runs a machine so that the log depends only on the program, the jobs, the
configuration and a seed. Two implementations of the machine (the Python core and the native
core, [native core](../native/README.md)) must give identical logs and records for the same
trace. Code:
`src/togeari/testing/trace/`, checks in `tests/testing/trace/`.

## Decisions

`Decision(kind, job, region, offset, key, lane, unit)`:

- `job` is the job key.
- `region` and `offset` name a statement by its region's semantic hash and its address minus
  the region's start, so a log does not depend on load order.
- `key` is the order key (a tuple); `()` for `admit` and `end`.

| Kind | From the bus event | `region`, `offset` | `lane` | `unit` |
|---|---|---|---|---|
| `admit` | `job.admit` | the entry | — | — |
| `fetch` | `stmt.fetch` | the statement | its lane (None inline) | — |
| `issue` | `stmt.issue` | the statement | the lane | — |
| `submit` | `lane.submit`, one decision per item of `work` | the statement | the lane | the unit |
| `writeback` | `stmt.done` | the statement | the lane | the unit, `inline`, `store` or `reused` |
| `squash` | `stmt.squash` | the statement | the lane | — |
| `retire` | `stmt.retire` | the statement | the lane | — |
| `fault` | `fault.taken` | the faulting statement | — | the error kind |
| `end` | `job.end` | the entry | — | `ok` or the failure kind |

`DecisionLog().attach(machine)` registers one synchronous bus listener for these kinds
([bus](../plugins/bus.md)). A listener runs on the dispatcher thread, so it sees the events in
the order they were emitted, and it is never dropped or sampled. It also makes `bus.wants`
true for every kind, so every event is built: a traced machine pays for its events.

| Method | Does |
|---|---|
| `digest()` | SHA-256 over one canonical JSON list per decision |
| `first_mismatch(other)` | `Mismatch(index, mine, other)` at the first differing index (a missing tail counts), or None |
| `counts()` | decisions per kind |
| `write(path)`, `DecisionLog.read(path)` | one JSON list per line |

## Records

`records.canonical(rec)` is a record with wall-clock fields left out:

- the key, entry, `ok`, failure and missing outputs;
- digests of the outputs, of the stats without `seconds` (`TIMED_STATS`), and of the
  conformance record.

`records_digest(records)` digests them in order. `first_difference(a, b)` gives the first
differing pair.

## The driver

`run_trace(machine_factory, jobs, seed=0, max_cycles=1_000_000)` returns
`Trace(log, records, cycles, clock_s, machine)`:

1. It builds the machine, and every lane unit must be a `ScriptedEngine` (otherwise a
   `TypeError` names the other types).
2. It binds each unit to one `Schedule`, sets a `VirtualClock` as the dispatcher's and every
   lane's `clock` ([cycle](../runtime/loop/cycle.md#the-clock)), and attaches the log.
3. Every job goes into the run path: a source buffer that is already finished, admission, and
   an unbounded sink queue. A plain dict becomes job `str(i)`.
4. On the calling thread, without a dispatcher thread, it repeats:
   - when the inbox is empty and no job is dirty or ready, it moves the clock to the earlier
     of the next engine answer and `now + next_timeout()`, then fires every answer due;
   - it posts a `("wake",)` when the inbox is still empty;
   - it runs `cycle()` and `after_cycle()`;
   - it stops when the run is done.
5. It raises `TraceStuck` when nothing is left to wait for, or after `max_cycles` cycles.

The collector is disabled during the run. Values are then freed by reference count only, at
points the program fixes, so held bytes and the admission that reads them are deterministic.
Afterwards the machine is stopped and the collector restored.

## Scripted engines

`ScriptedEngine(kind, name, latency_ms=1.0, tail=1.0, capacity=8, max_batch=16,
item_error_rate=0.0, outages=None)`, and the shortcuts `ScriptedCall`, `ScriptedBatch`
(capacity 2) and `ScriptedStream` (capacity 256). It has no thread.

- **Submit.** A submit draws its delay, `latency * (0.5 + U) * (1 + tail * U'^4 * 20)`, and
  each item's failure, from `random.Random(f"{seed}/{name}")`, in submit order. It schedules
  the answer at `now + delay`.
- **Answer.** When the answer fires, the payload runs on the driver's thread.
- **Ties.** Answers due at the same time fire in submit order.
- **Outages.** `outages` are `(start, end)` in virtual seconds. Inside one, `ready()` is false,
  `capacity()` is 1 and a submit is answered `UnitError(SimOutage)` at once.
- **Cancel, stop and refusal.** `cancel` drops a pending answer and answers `Cancelled()`.
  `stop` answers `UnitError(EngineStopped)`. A submit outside a trace raises `RuntimeError`.

## Determinism in the runtime

A log is only as deterministic as the runtime's own orders:

- jobs are fetched and retired in the order they became dirty (`dirty` is an
  insertion-ordered dict);
- phis are made in name order, so phi resolution and the waiters it wakes do not depend on
  string hashing;
- strands, inflights and lane queues are ordered by order key, sequence numbers or arrival.

Job and slot ids differ between runs in one process, so no log field contains one.

## Checks

`tests/testing/trace/test_trace_determinism.py`, on 16-job generated programs over scripted
lanes. `trace_machines.generated` binds the [generator's](generated-programs.md) nodes like
`conftest.build`, with admission `Fixed(8)` and every job recorded. By default the runs inject
item failures (5%) and an outage of unit 0 (4–20 ms).

| Test | Checks |
|---|---|
| `test_one_job_makes_the_decisions_of_the_protocol` | one job of two dependent calls gives exactly `admit, fetch, fetch, issue, submit, writeback, issue, submit, retire, writeback, retire, end` |
| `test_the_tiny_trace_is_pinned` | the log digest of a two-job trace equals a pinned value |
| `test_one_seed_gives_one_trace` | for 8 programs, two runs give no mismatch in log or records, and the same cycles and virtual time |
| `test_one_seed_gives_one_trace_in_a_fresh_process` | a child process with another `PYTHONHASHSEED` prints the same log and record digests |
| `test_other_seeds_give_other_schedules_and_the_same_results` | seeds 1 and 2: the outputs agree for every program; the logs differ for all but at most 2 of 8 (a program whose jobs share one batch has one order) |
| `test_traced_records_conform` | every traced job conforms by re-execution |
| `test_a_swapped_decision_is_found_at_its_place` | two adjacent decisions swapped: the mismatch is at that index and the digest changes |
| `test_a_dropped_or_added_decision_is_found` | a log one shorter or longer mismatches at its end |
| `test_another_issue_order_shows_in_the_log` | `DeepestFirst` instead of `OldestJobFirst` at the same seed: another log, the same outputs |
| `test_the_log_file_reads_back_as_written` | `write` then `read` gives an equal log and digest |
| `test_only_scripted_engines_are_driven`, `test_a_scripted_engine_refuses_work_outside_a_trace`, `test_a_run_past_its_cycle_cap_is_reported` | the refusals |
