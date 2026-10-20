# Togeari: repo notes

A dataflow machine that runs many KIR jobs at once, out of order, on pluggable engines.

## Layout

- `src/togeari/core/`: the machine.
  - `contracts/`: engines, work items, adapters, policies, IO
  - `program/`: load, job program per entry, code memory, decode tables, bind
  - `runtime/`: dispatcher, front end, job state, snapshots
  - `lanes/`: queues and multi-issue to engine units
  - `policies/`: admission, issue order, formers, selectors
  - `plugin/`: plugin manager, query / command tables
  - `observe/`: event bus
  - `construct/`: `Machine`, construction errors, core selection (`core.py`)
- `src/togeari-rust/`: the native core `togeari._rs` (crate `Cargo.toml` at the root); modules
  mirror `core/`, each with `core.rs` (pure Rust, unit tests) and `python.rs` (bindings).
- `src/togeari/engines/`: builtin engines (python, web, torch), written against the public contracts.
- `src/togeari/plugins/`: builtin plugins (counters, run report, perfetto, recorder, inspector, budget).
- `src/togeari/{adapters,stores,io,conformance,testing,cli}/`
- `examples/`: `hello`, `multi_unit`, `trace_replay`, `parallel`, `mixed`.
- `plans/` (gitignored): spec, engine notes, phases, decisions, STATE.md.

## Commands

The environment is `.venv`. Use `uv` to install into it; the install builds the extension
(maturin), so a change under `src/togeari-rust/` needs `uv pip install -e ".[dev,web,vault]"`
again. The version lives in `Cargo.toml` alone.

```
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test --no-default-features
.venv\Scripts\python.exe -m pytest tests -q -m "not gpu"
.venv\Scripts\python.exe -m pytest tests -q -m gpu
.venv\Scripts\python.exe -m black --line-length 100 src tests examples scripts
.venv\Scripts\python.exe -m ruff check src tests examples scripts
.venv\Scripts\python.exe scripts\check_imports.py
.venv\Scripts\python.exe scripts\check_md_black.py .
.venv\Scripts\python.exe examples\hello\machine.py
```

`-m "not gpu"` is the CPU-only run; tests that need a CUDA device carry the `gpu` marker and
run on their own with `-m gpu`. `TOGEARI_SEEDS=300` widens the generated-program checks
(default 20).

## Rules

- `togeari.core` imports only `kohakunode`, the standard library, `togeari._rs` and itself.
  Engines and plugins use the public contracts.
  `scripts/check_imports.py` checks this.
- Code style:
  - blank lines between logical groups;
  - a short comment above a group says what it does;
  - `match` rather than long if / elif chains;
  - no imports inside functions;
  - ruff + black, line length 100.
- Python blocks in markdown pass black.
- Long runs go to background jobs. Tests stay cheap: seconds each.
