# Validating a compatible KIR and Togeari pair

Togeari 0.3.0 requires `kohakunode>=0.3.0`. Do not lower this minimum to make an
unpublished dependency install: earlier KIR versions lack required contracts.

## Development and CI

The test workflow pins `KIR_REVISION` to a full immutable KohakuNodeIR commit.
It checks out that revision, builds and installs its wheel, then builds and
installs Togeari with test extras. This validates source integration without
requiring a package-index release or silently following another repository's main.
Update the pin deliberately when validating another pair.

After installation, `scripts/check_installed_pair.py --kir-revision <full-sha>`
prints versions and exact imported package/native-extension paths. It requires
both files to belong to the installed wheel's RECORD and the same package
directory; editable checkouts intentionally do not pass this wheel-only check.
The SHA argument records the revision selected by the preceding checkout/build
step; it is not a cryptographic attestation of arbitrary preinstalled wheels.

A local equivalent, using fresh wheel/output directories and an isolated venv:

```bash
# In the pinned KohakuNodeIR checkout:
uv build --wheel --out-dir ../pair-wheels/kir
# In the Togeari checkout:
uv build --wheel --out-dir ../pair-wheels/togeari
uv venv ../pair-venv
uv pip install --python ../pair-venv/bin/python ../pair-wheels/kir/*.whl
uv pip install --python ../pair-venv/bin/python '../pair-wheels/togeari/togeari-<wheel>.whl[dev,web,vault]'
../pair-venv/bin/python scripts/check_installed_pair.py --kir-revision <full-sha>
```

On Windows use the venv's `Scripts/python.exe` and expand wheel filenames with
the shell. Run both cores' CPU suites; retain separate serial passes:

```bash
python -m pytest tests -q -m 'not gpu and not serial' -n auto --maxprocesses 16 --dist worksteal
TOGEARI_CORE=python python -m pytest tests -q -m 'not gpu and not serial' -n auto --maxprocesses 16 --dist worksteal
python -m pytest tests -q -m 'serial and not gpu'
TOGEARI_CORE=python python -m pytest tests -q -m 'serial and not gpu'
```

KIR's workflow retains the supported Python/OS matrix, broad native parity, Rust,
editor, and Android gates. Its focused module/record/collect parity scripts get
an explicit absolute extension path. Its WASM checks execute the Node wrapper
built in the same job, not merely a compile/lint check.

## Release order

1. Validate the paired source revisions and normal release gates.
2. Publish the compatible KIR wheel/sdist release and verify index availability.
3. Publish Togeari retaining its minimum compatible KIR requirement.

A successful paired source run does not establish that either package is on the
index. Branch pushes do not require tags or package publication. Remote matrix
jobs not yet executed must be reported as outstanding, not local passes.
