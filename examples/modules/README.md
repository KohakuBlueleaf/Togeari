# Imported library on both cores

Run from the repository root with the current Togeari and KohakuNodeIR checkouts
installed. The Rust command requires the native extension; Python needs no Rust
core at runtime.

```sh
python examples/modules/machine.py --core both
python examples/modules/machine.py --core python
python examples/modules/machine.py --core rust
```

The default is `both`, not an automatic fallback. Each core runs three jobs
(mixed, empty and filtered inputs), checks the expected outputs, then checks
recorded replay **and reexecution** against the reference interpreter. Expected
summary for each core:

```text
conformance: 3/3 replay + reexecute match reference
origins: same definition node, two distinct call-site chains
```

`main.kir` imports the exported `transform` definition from `transforms.kir`
and calls it twice. The library's `each` and branch discard negative numbers;
its private `decorate` definition labels the remaining values. The two calls
produce `primary` and `backup` labels. All host implementations are tiny,
deterministic Python functions in `module_demo_nodes.py`, bound inline by
library identity (`module_demo.*`). No GPU, service or extra package is needed.
The imported body stays visible to lowering, decoding and scheduling rather
than becoming one opaque host call.

## Machine CLI and archived conformance

`build()` also follows the machine-module CLI contract:

```sh
togeari check examples/modules/machine.py
togeari run examples/modules/machine.py --report runs/modules
PYTHONPATH=examples/modules togeari conform runs/modules --lib module_demo_nodes:LIB
```

The CLI uses the normal `auto` core selection; use `TOGEARI_CORE=python` or
`TOGEARI_CORE=rust` to override it. The direct Python runner above explicitly
selects both cores and also performs reexecution. The archive replay command
needs the host library on `PYTHONPATH`; in PowerShell use:

```powershell
$env:PYTHONPATH = "examples/modules"
togeari conform runs/modules --lib module_demo_nodes:LIB
```

The module directory is relocatable as a unit: imports are resolved relative
to `main.kir`.

## Where origins actually live

The runner prints an actual entry from `record.stats["record_origins"]`. This
list is aligned with recorded calls; it does **not** add fields to the legacy
five-element call record. The four `decorate` calls for the mixed input share
one source node in `transforms.kir`, but their `calls` ancestry distinguishes
the two sites in `main.kir`. Source definition, module and line survive private
name mangling. Repeated loop iterations at one site share a static origin;
the individual record position identifies the execution occurrence.

Compiled instructions carry the same provenance in their spans. Archive
program metadata stores origins separately from linked executable text, plus
a dependency manifest and authored text, so replay does not need the original
imported `.kir` files. Fault provenance, when present, is in
`record.stats["fault_origin"]`; the ordinary failure dictionary still contains
only `kind`, `node` and `message`. Origins are not extra KIR syntax, and origin
changes alone do not change semantic identity. Fallback source IDs are
traversal paths: formatting-stable, not stable under arbitrary source edits.
