# Engine checks

`check_engine` in `src/togeari/testing/check.py` drives any engine the way a dispatcher does and
checks the behaviour rules B1–B7 listed in [writing an engine](../../guides/writing-an-engine.md).
`togeari units machine.py` runs it on every unit of a machine.

## Call

```python
report = check_engine(factory, sample_items, failing_items=(), kill=None, tolerance=None)
report.raise_if_failed()
```

| Argument | Default | Meaning |
|---|---|---|
| `factory` | — | returns the engine; `check_engine` calls `start()` and waits for `ready()` |
| `sample_items` | — | `WorkItem` templates that must succeed; each submit uses a fresh copy |
| `failing_items` | `()` | templates that must give `ItemError` |
| `kill` | `None` | kills the backend the way a crash does; the next submit must give `UnitError` |
| `tolerance` | `None` | relative tolerance, or `fn(a, b) -> bool`, for output comparisons |
| `submit_bound_ms`, `submit_quantile`, `submit_max_ms` | 2.0, 0.99, 50.0 | Legacy diagnostic references only; elapsed time never affects the behavioral verdict |
| `rounds`, `cancel_rate` | 200, 0.25 | submits of the B2 / B5 phase; the share cancelled |
| `timeout_s`, `grace_s`, `seed` | 30, 0.2, 0 | answer wait, late-duplicate grace, RNG seed |
| `kill_wait_s` | 2.0 | after `kill()`, the wait for `ready()` false or `capacity()` 0 |
| `probe_s` | 1.0 | how long a probe holds `done` open |

Every engine call is made from one driver thread (`check-driver`); `done` may arrive on any
thread.

## Instrumentation

Before the run, `_Driver.instrument()` wraps the engine's `DRIVER_CALLS` (`submit`, `cancel`,
`capacity`, `load`, `key`, `wake_at`) on the instance to record the calling thread; `restore()`
removes the wrappers afterwards. A sample whose adapter is not `light` is wrapped (`_Watched`)
so a `prepare` on the driver thread is counted.

## Steps

`_run` calls one function per step, each adding its verdict to the rule table:

1. **Reference** (`_reference`). Each sample alone; its output is the reference.
2. **B2 / B5** (`_rounds`, then `_b2`, `_b5`). `rounds` submits within capacity (random
   batches of one key group on batch engines); half the cancels right after submit, half up to
   5 ms later; `load()` every 16 submits.
3. **B4** (`_b4`). At capacity, `capacity() + 1` submits at once.
4. **B6** (`_b6`). Batch engines: every key group shuffled into full batches.
5. **B3 probe.** For up to three samples (`_probe`): `done` is held open on the engine's thread
   while the driver calls `capacity()`, `load()`, `submit()` and `cancel()`; those calls must
   return within `probe_s`, so `done` is never called under a lock the engine's own methods take.
6. **B7** (`_b7`). Each failing item; then `kill()`, a wait of up to `kill_wait_s` for the
   engine to notice, and one more submit.
7. **B1** (`_b1`). Reject inline successful results; report submit times as diagnostics only.
8. After `grace_s`: exactly-once accounting; then B3.

| Rule | Passes when |
|---|---|
| B1 | no `Ok` is delivered inside `submit`; timing measurements are diagnostic only, not machine-dependent pass/fail gates |
| B2 | every item answered; uncancelled items `Ok`; no item answered twice; non-batch outputs equal their reference |
| B3 | every instrumented call came from the driver thread, no heavy-adapter `prepare` ran on it, every probe returned, no call raised, every `done` delivered a list of results of the right length |
| B4 | idle capacity is above 0 and every item of the over-capacity burst is answered `Ok` |
| B5 | a cancelled item's answer is `Cancelled()` or a normal result equal to its reference; never twice |
| B6 | batch outputs equal the outputs alone (n/a for other kinds) |
| B7 | failing items give `ItemError`; after `kill`, `UnitError` (n/a when neither is given) |

B8 (equal payloads are separate requests) is not checked: how many backend requests an engine
makes is not visible through the engine interface. One answer per item, equal payloads included,
is B2.

`EngineReport.rules` holds one `RuleResult(rule, ok, detail)` per rule; `ok`, `failed()` and
`str(report)` summarise it.
