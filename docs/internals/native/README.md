# Native core

The Python runtime under `src/togeari/core/` is the reference implementation of the machine:
it defines every decision the dispatcher makes, every event and every hook point. The native
core is a second implementation of the same protocol in Rust, built into the same wheel as the
extension module `togeari._rs`. `Machine(core="rust")` runs it; the two cores make the same
decisions in the same order on the same events ([parity](parity.md)).

| Page | Covers | Code |
|---|---|---|
| [Dispatcher core](core.md) | the state model, memory, the stages, builtin policies, digests, events, views | `src/togeari-rust/{program,runtime,contracts}/`, `core/runtime/native.py` |
| [Native lanes](lanes.md) | adoption, settings, queues, builtin policies, callbacks | `src/togeari-rust/lanes/`, `core/lanes/native.py` |
| [The GIL](gil.md) | the inbox, GIL-free regions, the region gate, what keeps the GIL | `runtime/{inbox,gate,cycle}.rs`, `runtime/frontend/fetch.rs`, `runtime/retire/retire.rs` |
| [Parity](parity.md) | what both cores share, the trace checks | `tests/testing/trace/test_core_parity.py` |

## Selection

`src/togeari/core/construct/core.py` picks the core of a machine:

| Asked (`core=`, else `TOGEARI_CORE`, else `"auto"`) | Runs |
|---|---|
| `"python"` | the Python core |
| `"auto"` | `AUTO` (`"rust"`), the core the measurements of spec/11 §11.6 chose ([performance](../../performance/native.md)); Python when the extension is not built or has no dispatcher |
| `"rust"` | the Rust core; without the extension, or one built without the dispatcher, a `ConstructionError` naming `core=` or `TOGEARI_CORE=` |
| anything else | a `ConstructionError` |

`Machine.core` is the core that runs. A Rust machine's dispatcher is a `NativeDispatcher` and
its lanes are adopted ([lanes](lanes.md)). `togeari doctor` prints the default machine's core
and the extension; the run report's `spec.json` carries `core`.

## Packaging

| File | Holds |
|---|---|
| `Cargo.toml` | the crate `togeari`, lib `_rs` at `src/togeari-rust/lib.rs`, deps `pyo3`, `indexmap`; the package version |
| `pyproject.toml` | the maturin build: `python-source = "src"`, `module-name = "togeari._rs"`, `dynamic = ["version"]` |
| `rustfmt.toml` | `max_width = 100` |
| `src/togeari/_rs.pyi` | stubs of the extension |
| `src/togeari/py.typed` | the package is typed |

The version lives in `Cargo.toml` alone; maturin writes it into the wheel's metadata in
PEP 440 form (`0.2.0-dev0` becomes `0.2.0.dev0`), and `togeari.__version__` reads that
metadata. `tests/machine/package/test_version.py` checks the mapping, the installed version
and the extension's own `__version__` against `Cargo.toml`. The sdist carries only the crate
(`Cargo.toml`'s `include`), the Python package, `pyproject.toml`, the README and the license.

## Crate layout

```
src/togeari-rust/
├── lib.rs              the #[pymodule] _rs (gil_used): registers every module's bindings
├── info/               the extension's description (version, DISPATCHER, core_info)
├── contracts/          the canonical digest
│   ├── hash.rs         SHA-256 and SHA-1
│   ├── digest.rs       the digest feed of builtin values; other values fed by Python
│   └── python.rs       togeari._rs.digest, digest_with_weak
├── program/            code memory as the core reads it
│   ├── core.rs         names, name sets, instructions, decode tables, regions, @try tables
│   └── python.rs       import of one region from the Python code memory
├── runtime/            the dispatcher
│   ├── state/          keys, slots, name maps, frames, strands, inflights, window, jobs
│   ├── frontend/       fetch and rename, control (park, joins), each
│   ├── backend/        readiness, issue, writeback
│   ├── retire/         retire, faults and squashes, snapshots
│   ├── pool/           job start and end, records, admission
│   ├── core.rs         the Core: pool, scheduling sets, counters
│   ├── host.rs         the Python side the core calls into, the knobs it reads
│   ├── builtins.rs     the builtin policy methods, told apart by the function they resolve to
│   ├── events.rs       bus events, result and error kinds
│   ├── gate.rs         the region gate
│   ├── inbox.rs        the native inbox
│   ├── cycle.rs        the cycle, the inbox drain, GIL-free regions
│   ├── traverse.rs     the collector's walk over the core, its teardown
│   └── python.rs       togeari._rs.Core, Owner, the Inbox's Python methods, live_objects
└── lanes/              native lanes
    ├── queue.rs        queues, buckets, ripe and time heaps (no Python call)
    ├── lane.rs         unit states, submissions, results, timers, health, attach / detach
    ├── pump.rs         unit order, admission, batch counts, taking, submitting
    ├── policy.rs       a lane's former and selector as one lane operation runs them
    └── python.rs       togeari._rs.Lane, Done, Bucket, Head
```

Modules mirror `src/togeari/core/`; each `python.rs` holds its module's Python classes and
functions. `lanes/queue.rs`, `contracts/hash.rs`, `runtime/gate.rs`, `runtime/state/` and
`program/core.rs` make no call into Python; the other files make the calls the Python core
makes at the same points (engines, policies written in Python, plugins, stores, values). Unit
tests sit next to the code (`program/core.rs`, `contracts/hash.rs`, `lanes/queue.rs`,
`runtime/state/{key,names,slot}.rs`, `info/core.rs`); the parts that call into Python are
checked through the Python suite and the [parity](parity.md) traces.

## Building

```
uv pip install -e ".[dev,web,vault]"          # builds the extension into src/togeari/
maturin develop --release                     # rebuilds it in place
maturin build --release --out dist            # a wheel
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test --no-default-features              # links libpython; on Windows its folder on PATH
```

The `extension-module` feature (on by default) leaves libpython unlinked, as a wheel needs;
`--no-default-features` links it so the unit tests run as a plain binary.

CI builds the wheel with maturin on every OS and Python version of the matrix, installs it and
runs the suite against it twice, on the default core (Rust) and with `TOGEARI_CORE=python`;
a separate job runs `cargo fmt`, `clippy` and `cargo test`. A `v*`
tag builds wheels for CPython 3.10–3.14 on Linux x86_64 and aarch64 (manylinux 2_28),
Windows x64 and macOS arm64, plus the sdist, and publishes them from environment `pypi`.
