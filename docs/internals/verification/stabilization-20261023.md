# Stabilization validation — 2026-10-23

## Tested revisions and changes

Baseline: KIR `7e27245c61ae1b47ebd5335f2a5031855fb3b7cb`, Togeari
`67a08987d450777907f1b523396389ec0e69e645`, both 0.3.0, clean working trees.
Only these repositories were changed. No public API or persisted-format change,
version bump, tag, package publication, or deferred architecture extension.

| Package | Commit | Result |
|---|---|---|
| F1 | Togeari `846fcce` | Failed event commitment is terminal `KirRuntimeError` on both cores |
| F2 | KIR `decab10` | Preserve explicit state hooks after slotted dataclass transformation |
| F3 | Togeari `9c50036` | Final deadline check after estimation/key/repacking, before attempt allocation |
| F4 | KIR `a9b2a64`, Togeari `661e5a6` | Pinned paired wheels, import ownership verification, focused native/actual WASM steps |
| F5 | Togeari `a3fb089` | Four bounded matching/stale continuation integration cases |
| F6 | Togeari `0abb991` | Reproducible measurements and documented retained-history/capture limits |

KIR's final tested source SHA is `a9b2a647379bf217cebf8e9a42375bc262fbd9df`.
Togeari runtime source was unchanged after `a3fb089`; later commits add CI,
measurements and documentation. The native extension was rebuilt after F3.

## Local evidence

Windows 11, development CPython 3.13.14, isolated CPython 3.10.20, Rust 1.97.1.
The development packages import from the intended `src/kohakunode` and
`src/togeari` checkouts with their `cp313-win_amd64.pyd` files. Separate wheels
were built with `uv build --wheel`, installed KIR-first into
`runs/validation/pair-env`, and verified using:

```bash
python scripts/check_installed_pair.py --kir-revision a9b2a647379bf217cebf8e9a42375bc262fbd9df
```

Both imports in that environment resolved to its `Lib/site-packages`, and both
Python/native files were owned by the installed wheels' RECORD entries. The
Windows conda-based build warned about an external zlib DLL; this local validation
is not a certification of wheel portability on a clean non-conda machine.

### Baseline reproductions and focused results

- F1: actual append-conflict/recovery-failure regressions under `@try` yielded
  **2 Python failures / 2 Rust passes** before editing. After the fix, event and
  runtime suites passed **62 tests** (2 serial tests excluded in that command).
- F2: existing state-replacement tests on Python 3.10 yielded **10 failures,
  217 passes** before editing. Existing suites plus compatibility tests then
  passed **234 tests on both Python 3.10 and 3.13**, including pickle protocols
  0–5. No native tests were silently skipped by those focused source-only suites.
- F3: new virtual-clock regressions failed **8/8** before editing (both cores,
  single/mixed batches, estimator/key clock advancement). Rebuilt both-core
  deadline/policy/resource suite passed **96 tests**, including all eight.
- F5: **4 passed**: matching and stale frontier on each core, imported source
  origins in bounded waits, reversed completion but ordered collect, independent
  progress, prefix identity/history preserved, no retired-prefix reexecution.
- Repaired KIR record/event integration in Togeari: **92 passed**.

### Full CPU commands and results

Run in Togeari, once for the default native core and once with `TOGEARI_CORE=python`:

```bash
python -m pytest tests -q -m 'not gpu and not serial' -n auto --maxprocesses 16 --dist worksteal
python -m pytest tests -q -m 'serial and not gpu'
```

Development environment: **2,524 parallel passed, 10 skipped; 25 serial passed,
per core**. Isolated paired-wheel environment (only declared dev/web/vault extras,
no optional Torch): **2,499 parallel passed, 17 skipped; 25 serial passed**, with
5 additional collection skips in each serial invocation. Seven expected
reexecution-compatibility warnings accompanied the parallel runs. An HTTP test
server occasionally logged a cancelled connection; pytest still passed.

KIR `python -m pytest tests -q`: **2,227 passed, 1 skipped**. Broad native/reference
parity: **936 sources, 44,680 comparisons, zero differences**. Focused commands
used the absolute imported `_rs.__file__`:

```bash
python rust/tests/parity/modules.py --extension "$KIR_NATIVE_EXTENSION"
python rust/tests/parity/records.py --extension "$KIR_NATIVE_EXTENSION"
python rust/tests/parity/collect.py --extension "$KIR_NATIVE_EXTENSION"
```

Results: **42 / 33 / 24 passed** respectively. Actual WASM release build used
locked dependencies and wasm-bindgen 0.2.129, with all three Node smoke scripts
passing against the absolute generated Node wrapper. CI installs the CLI
version read from Cargo.lock and supplies that same wrapper explicitly.

### Other gates

- Togeari: `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`,
  `cargo test --no-default-features` (**19 passed**).
- KIR: cargo fmt, all-target Clippy, full release Rust tests passed.
- Togeari Black **380 files**, Ruff, import boundaries and documentation code
  checks passed. KIR configured Black **100 files** and Ruff passed, including
  focused parity scripts and editor backend.
- Tree-sitter: **36 parses**, three highlight fixtures/**41 assertions** passed.
  VS Code grammar passed with CI-matching Node **22.23.4**. Ambient Node 26
  initially reported grammar success then failed in libuv teardown; that run
  is not counted as a pass. The explicit Node 22 rerun exited zero.
- Editor backend: **70 passed**; frontend production build passed.
- Live event example: three committed/acknowledged events and **2/2** conformance;
  hello example **5/5** conformance.

## Measurements and limits

See [event-history report](../../performance/event-history.md) and its raw JSON:
24 fresh-process samples, two repetitions, both cores, `record=None`, 128/512/2048
events, 256/4096-byte payloads. Delivery backlog was zero in every sample.
Retained history/artifacts grew approximately linearly; synchronous encoding and
packing, not just disk acknowledgment, delayed independent jobs. No production
optimization, new ownership model or snapshot format was introduced.

Preserved limits: finite eager collect and whole-program declared-mutation
exclusion; progressing collection finishes before capture; older event frontiers
are rejected; portable immutable records; estimated rather than reserved
resources; at-least-once delivery; full event history retained and serialization
synchronous. No GPU/model/external-service workload was required.

## Remote validation and publication boundary

The local runs above do **not** claim Linux/macOS, all supported Python matrix
jobs, Android cross-compilation, or remote GitHub Actions have passed. Those jobs
remain enabled and execute after the final branch pushes. Remote results must be
reported separately, with any failure or unavailable run explicit. Publishing
packages is outside this round; KIR must be published before a future Togeari
release while retaining `kohakunode>=0.3.0`.
