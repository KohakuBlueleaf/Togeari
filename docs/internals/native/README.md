# Native core

The Python runtime under `src/togeari/core/` is the reference implementation of the machine:
it defines every decision the dispatcher makes, every event and every hook point. The native
core is a second implementation of the same protocol in Rust, built into the same wheel as the
extension module `togeari._rs`.

## Status

The extension is built and shipped. It carries its version and `core_info()`; it holds no
dispatcher yet (`togeari._rs.DISPATCHER` is `False`), so every machine runs the Python core.

## Packaging

| File | Holds |
|---|---|
| `Cargo.toml` | the crate `togeari`, lib `_rs` at `src/togeari-rust/lib.rs`; the package version |
| `pyproject.toml` | the maturin build: `python-source = "src"`, `module-name = "togeari._rs"`, `dynamic = ["version"]` |
| `rustfmt.toml` | `max_width = 100` |
| `src/togeari/_rs.pyi` | stubs of the extension |
| `src/togeari/py.typed` | the package is typed |

The version lives in `Cargo.toml` alone; maturin writes it into the wheel's metadata in
PEP 440 form (`0.2.0-dev0` becomes `0.2.0.dev0`), and `togeari.__version__` reads that
metadata. `tests/machine/package/test_version.py` checks the mapping, the installed version
and the extension's own `__version__` against `Cargo.toml`.

The sdist carries only the crate (`Cargo.toml`'s `include`), the Python package, `pyproject.toml`,
the README and the license.

## Crate layout

```
src/togeari-rust/
├── lib.rs        the #[pymodule] _rs: registers every module's bindings
└── info/         the extension's description
    ├── core.rs   VERSION, DISPATCHER, CoreInfo, core_info(); unit tests
    └── python.rs __version__, DISPATCHER, core_info() -> dict
```

Modules mirror `src/togeari/core/`. Each splits pure Rust (`core.rs` and siblings, unit tests
next to the code) from its bindings (`python.rs`, with a `register(m)` that `lib.rs` calls).

## Selection

`src/togeari/core/construct/core.py` picks the core of a machine:

| Asked (`core=`, else `TOGEARI_CORE`, else `"auto"`) | Runs |
|---|---|
| `"python"` | the Python core |
| `"auto"` | Rust when `togeari._rs` imports and holds the dispatcher, else Python |
| `"rust"` | Rust; without the dispatcher, a `ConstructionError` naming `core=` or `TOGEARI_CORE=` |
| anything else | a `ConstructionError` |

`Machine.core` is the core that runs. `togeari doctor` prints the default machine's core and
the extension; the run report's `spec.json` carries `core`.

## Building

```
uv pip install -e ".[dev,web,vault]"          # builds the extension into src/togeari/
maturin build --release --out dist            # a wheel
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test --no-default-features              # links libpython; on Windows its folder on PATH
```

The `extension-module` feature (on by default) leaves libpython unlinked, as a wheel needs;
`--no-default-features` links it so the unit tests run as a plain binary.

CI builds the wheel with maturin on every OS and Python version of the matrix, installs it and
runs the suite against it; a separate job runs `cargo fmt`, `clippy` and `cargo test`. A `v*`
tag builds wheels for CPython 3.10–3.14 on Linux x86_64 and aarch64 (manylinux 2_28),
Windows x64 and macOS arm64, plus the sdist, and publishes them from environment `pypi`.
