# KIR modules and source origins

Togeari links KIR modules before selecting an entry and applying its normal
`order_dataflow`, `inline`, `strip_meta` passes. Imported subgraphs remain ordinary KIR
control flow and host-node calls: modules do not add a runtime scheduling mechanism.
Both `core="python"` and `core="rust"` carry the same provenance.

## A reusable module

Save this as `arithmetic.kir`:

```kir
@export increment
@def (x: int)helper(y: int):
    (x, 1)add(y)
@def (x: int)increment(y: int):
    (x)helper(y)
```

Save the entry as `main.kir`:

```kir
@import "arithmetic.kir" as arithmetic
@def (x: int)main(y: int):
    (x)arithmetic.increment(a)
    (a)arithmetic.increment(y)
```

```python
from pathlib import Path

from togeari import Inline, Machine
from togeari.plugins import Recorder

with Machine(
    program=Path("main.kir"),
    entry="main",
    bind={"kir.std.*": Inline()},
    record=1.0,
    plugins=[Recorder("records")],
) as machine:
    result = machine.submit({"x": 3}).result(5)
    assert result.outputs == {"y": 5}
    print(result.stats["record_origins"])
```

`@import` loads KIR declarations; `@use "host.library" as alias` still loads a host
signature library. Modules explicitly export definitions. Private names may repeat in
different modules; the linker generates deterministic identifiers and unique host-library
aliases. Binding patterns can use the original library-qualified host node name, such as
`kir.std.add` or `my_library.lookup`, regardless of those aliases. Recorded call names are
the linked spellings, shared by the machine and reference interpreter. The same contract
applies to a caught error's `node` field: moving a host call into an imported module may
change that linked spelling. Use the separate source origin for source attribution; it
never changes the error value seen by KIR code.

A `Path` program resolves relative module imports from its parent directory. Its logical
source name is the filename, not the absolute checkout path. Moving the whole project
without changing relative names or contents therefore preserves dependency identity.
For filesystem-free compilation, KIR also accepts
`compile_source(text, modules={"arithmetic.kir": module_text}, source_name="main.kir")`;
Togeari's machine construction API continues to accept paths or source text, not a module
map directly. Use a path for a multi-file machine program.

## Program identity and replay

A `JobProgram` retains:

- `source_program` and `text`: the authored entry program and text;
- `written`: the **linked, checked, unlowered** program, used by reference replay;
- `program`: the selected entry after the requested passes;
- `linked_text`: the canonical self-contained KIR representation of `written`;
- `dependencies`: KIR's versioned dependency manifest;
- `origins`: independent `written` and `lowered` provenance sidecars.

The manifest identifies logical modules, module semantic hashes, import aliases, exports
and host signature-library hashes. Its fingerprint is mixed into the job's semantic and
full hashes. Programs with no module declarations retain the legacy empty `{}` manifest
and hash construction. Source origins do **not** enter semantic identity.

Code memory shares a semantic-hash cache hit only when both source attribution and origin
sidecars match. A metadata-only variant gets a distinct immutable region with the same
semantic hash, leaving running instructions and their provenance intact. The `by_hash`
lookup remains a representative region, not a complete provenance index. Records identify
the exact region in `stats["program_region"]`. This region number is transient and never
enters the dependency manifest or semantic program hash. Source origin is not an execution or
invocation ID: recorded call order, job keys and dynamic runtime keys remain separate.

## Structured origins

Every stamped KIR statement has an optional origin with these JSON fields:

| Field | Meaning |
|---|---|
| `module` | portable logical source module |
| `node_id` | authored graph/editor ID when supplied, otherwise a structural text fallback |
| `line` | original source line, or null |
| `definition` | original definition name, or null |
| `calls` | outermost-first call-site origins introduced by inlining; compiler-produced entries are shallow, with empty `calls` |
| `generated` | generated-operation marker, or null |

Two expansions of one definition share its definition origin but have different call-site
ancestry. Text fallback IDs are structural paths: they survive formatting but are not a
promise of stable identity through arbitrary source edits. Origins survive `strip_meta`.
The decoder uses structured origins first; generated-name matching is only a fallback for
an unstamped AST. Its synthetic iteration-end instruction derives its origin from the
owning `each`, with `generated="iter_end"`.

Origins are available through:

- decoded `Instr.span.origin`, with readable module/definition/call sites in decode dumps;
- the Inspector `programs` query: `dependencies` and `origins` indexed by instruction address;
- the Inspector `job` query: `origin` beside each strand/window row's existing `at` string;
- `JobRecord.stats["record_origins"]`: a list aligned one-to-one with `JobRecord.record`;
- `JobRecord.stats["fault_origin"]`: the last taken fault's origin;
- `on_fault` notifications and `fault.taken` events: a top-level `origin` field.

A missing origin is null. Recording limits discard the origin list when they discard the
call records. Restoring a legacy snapshot with call records but no origins pads its old
prefix with nulls before appending new origins. Provenance never changes the KIR error
value delivered to an `@except` handler, or the five-field call-record tuple.

## Archive encoding and compatibility

`programs/<hash>.json` now has `schema_version: 2`. Existing fields remain, with `text`
containing linked checked KIR rather than an unresolved entry source. Additions are:

- `authored_text`: the original entry source, for display;
- `dependencies`: the manifest, including its version and fingerprint;
- `origins.written` and `origins.lowered`: each
  `{"version": 1, "statements": {"body/0/body/1": origin, ...}}`.

Paths identify statements in the respective AST, with `body` and `handler` segments.
They are sidecar attachment locations, not replacements for the origin's `node_id`.
Job entries retain their existing layout, including five-field call tuples; dynamic
origins live in the existing `stats` mapping.

Each run also retains `runs/<run>/programs/<hash>.json` and exact loaded-region variants
`runs/<run>/programs/<hash>-<region>.json`. `Archive.program(hash, run, region=None)` and
replay prefer exact region provenance, then run-local metadata, then the legacy global
index. This prevents both another run and another loaded region with identical code but
different metadata from overwriting a job's provenance. `Archive.programs` remains the
legacy global index; recorded job stats carry the run-local `program_region` selector.

Replay validates the manifest fingerprint and loaded signature contracts, rebuilds from linked
text, restores the manifest for hash verification and reattaches the sidecars. It never
reopens source module files.
Deleting or changing those files after recording cannot change the replayed program.
Host libraries and digest providers must still be importable, as described in
[Conformance](conformance.md); this is not a host implementation or environment bundle.

The reader accepts legacy program JSON without a schema version, manifest or origins.
Unknown schema/sidecar versions are rejected rather than guessed. Old releases are not
promised to understand dependency-aware hashes produced by new module programs.
Archive directories remain trusted inputs: job records are pickles.
