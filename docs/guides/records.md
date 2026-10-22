# Typed immutable records

KIR libraries can declare portable record schemas. Togeari executes their generated
constructors, projections and updates as ordinary node calls: they use the existing
bindings, lanes, adapters, stores and conformance machinery. There is no record-specific
scheduler, engine protocol or placement rule. Both Python and Rust cores support them.

## Declare a schema and call its operations

```python
from kohakunode import NodeLibrary
from kohakunode.records import project

from togeari import Inline, Machine

lib = NodeLibrary("geometry")
Point = lib.record("Point", [("x", int), ("y", int, 0), ("tags", "list[str]", [])])

program = """@use "geometry" as g
@def (x: int)main(original, moved, old_x, new_x):
    (x=x, tags=["sample"])g.Point.make(original)
    (original, 10)g.Point.with.x(moved)
    (original)g.Point.get.x(old_x)
    (moved)g.Point.get.x(new_x)
"""

with Machine(
    program=program,
    entry="main",
    libraries=[lib],
    bind={"geometry.Point.*": Inline()},
) as machine:
    result = machine.submit({"x": 3}).result(5)
    assert result.ok
    assert result.outputs["old_x"] == 3
    assert result.outputs["new_x"] == 10
    assert project(Point, result.outputs["original"], "tags") == ["sample"]
```

`NodeLibrary.record(name, fields)` accepts ordered `(name, type)` and
`(name, type, default)` tuples, or explicit `RecordField` objects. A third tuple item means
a default is present, even when it is `None`. A nullable field without a default must still
be supplied; missing and null are different.

The lower-level API is
`Library.add_record(RecordSchema(name, tuple_of_fields))`, returning the canonical schema.
`RecordField(name, type, has_default=False, default=None)` takes a KIR `TypeExpr`.
Registration qualifies local record references by the library's name. For example,
`Point.schema_id` is `geometry.Point`, independent of its source alias `g`.

| Generated node | Inputs | Output |
|---|---|---|
| `Point.make` | schema fields, in declaration order; defaults may be omitted | one `geometry.Point` |
| `Point.get.x` | `record: geometry.Point` | the declared type of `x` |
| `Point.with.x` | `record: geometry.Point`, `value: type_of_x` | a new `geometry.Point` |

Every field gets a `get` and `with` node. These are visible calls with signatures, not
arbitrary property access or assignment syntax. Generated operations are pure and have no
`mutates` or `ordered` flags. Bind by the original library-qualified name, such as
`geometry.Point.*`, even when KIR module linking generates a unique alias.

## Value and mutation rules

Python runtime values are `kohakunode.records.RecordValue`. Construct them with
`construct(schema, **fields)` or `RecordValue(schema, **fields)`; project with
`project(schema, record, field)`; update with
`replace_field(schema, record, field, value)`.

The initial portable domain is finite JSON-like values plus nested records:

- exact built-in strings, integers, booleans, finite floats and `None`;
- lists and string-keyed dictionaries, recursively containing portable values;
- nested nominal records; fields can use record types, optional types, unions,
  `list[T]` and `dict[str, T]`.

Records are nominal: identical field names do not make `geometry.Point` interchangeable
with `other.Point`. Schema validation, static checking and runtime operations reject
incompatible types. Arbitrary Python objects, container subclasses, property-backed host
objects, cycles and nonfinite floats are not record fields. There is no implicit
conversion from a plain dictionary to a nominal record.

Construction snapshots mutable containers. Projection materializes fresh lists and
dictionaries, including their nested mutable containers. Updating returns a new validated
snapshot; it does not mutate the old record. Immutable nested records may be shared.
Thus a host node may safely mutate a projected list without changing either the source
record or another job's projection. Likewise, changing the input list after construction
cannot change the constructed record. This does not relax KIR's existing mutation rules
for ordinary host-node inputs: declare `mutates` when a host node changes a list it receives.

Record defaults are portable JSON values copied into immutable schema storage; they are
not shared mutable class attributes. Nested record defaults are not supported in this
initial slice: construct the nested record explicitly and pass it to the outer constructor.

## Engines do not need special record scheduling

Small record operations are usually suitable for `Inline()`. They can also use an ordinary
Python worker engine:

```python
from kohakunode import NodeLibrary

from togeari import Machine, Use
from togeari.engines.python import PythonCallEngine

lib = NodeLibrary("geometry")
lib.record("Point", [("x", int), ("y", int, 0)])

with Machine(
    program='@use "geometry" as g\n@def (x: int)main(p):\n    (x)g.Point.make(p)\n',
    entry="main",
    libraries=[lib],
    bind={"geometry.Point.*": Use(PythonCallEngine(workers=1, mode="thread"))},
) as machine:
    assert machine.submit({"x": 4}).result(5).ok
```

Generated implementations are pickle-safe `RecordOperation` descriptors. They work with
`PythonCallEngine(mode="process", transport="pickle")` too. As with other process-engine
programs, launch scripts under an `if __name__ == "__main__":` guard; host functions must
remain importable by the worker. Pickle is a trusted local transport, not an untrusted wire
format. No torch, web or application engine is required to adopt `RecordValue` internally.

The [records example](../../examples/records/machine.py) constructs/projects records inline,
runs a simulated slow translation operation on worker threads, verifies the original record
is unchanged, checks replay/re-execution, and demonstrates the explicit JSON codec:

```text
python examples/records/machine.py
```

## Explicit adapter boundary

Use `encode_record(value)` and `decode_record(payload, schemas)` at external boundaries.
`schemas` maps canonical IDs to trusted `RecordSchema` objects. The codec encodes records,
lists and dictionaries using explicit tags, so a user dictionary containing a `$kir` key
cannot masquerade as a record envelope. A payload selects only a schema already in the
registry; decoding never imports a class or reflects over properties.

Existing `JsonHttp` callbacks are sufficient; no new engine adapter protocol is needed:

```python
import json

from kohakunode import NodeLibrary
from kohakunode.records import construct, decode_record, encode_record

from togeari.adapters.http import JsonHttp

lib = NodeLibrary("geometry")
Point = lib.record("Point", [("x", int), ("y", int, 0)])
schemas = {Point.schema_id: Point}


def build_request(inputs):
    return encode_record(inputs["point"])


def parse_response(payload):
    return decode_record(payload, schemas)


adapter = JsonHttp("/translate", build=build_request, parse=parse_response)
point = construct(Point, x=3)
request = adapter.prepare({"point": point}, "http")
wire = json.dumps(request.json, allow_nan=False)
assert parse_response(json.loads(wire)) == point
```

Bind a declared host operation with `Use(web_engine, adapter=adapter)`. The engine sees
an HTTP request and response, not a record class. The service implements the explicit
wire contract; it must return the tagged envelope, not a bare field dictionary. The same
approach works for other protocols with adapter `prepare`/`decode` callbacks. Reject
unknown schema IDs, extra/missing fields and incompatible values at the boundary rather
than coercing them silently. This guide's adapter snippet does not start a network service.

## Signatures, identity, recording and persistence

A signature library containing records uses `.kirsig.json` format **2**. Schema declarations
are serialized; loading reconstructs the generated make/get/with signatures and their
builtin implementations without importing host functions. Ordinary host signatures in the
same file still need implementations or engine bindings. A schema-only library therefore
can execute generated record operations even when no host implementation module is loaded.
Legacy libraries without records retain signature format 1.

Record contracts participate in dependency identity, including for programs with no KIR
module imports. These programs carry a dependency manifest with an empty `modules` list
and signature contract fingerprints. Changing a field type, default or schema changes
program identity even when the authored calls are unchanged. Legacy programs without
records or module dependencies keep their empty manifest and existing identity.

`RecordValue` supplies a strong content digest including nominal identity and tagged
values. Pickle, content-store copies and job snapshots preserve record values. There is
no type-only weak-digest fallback for valid records. Content-store bindings use the normal
`Use(..., store="content")` mechanism; schemas and host contracts are part of program
identity, not an engine's implicit global registry.

Recording and replay use the existing archive layout, five-field call tuples and origin
sidecars. Replay checks archived signature contracts and reconstructs the dependency-aware
hash exactly once, whether the program was record-only or linked from multiple KIR modules.
Re-execution runs generated operations like any other pure nodes. Host implementation and
signature files must still be available as described in [Conformance](conformance.md).

Snapshots obey the existing eligibility rules; records do not make an otherwise ineligible
run-ahead state checkpointable. A snapshot is still a trusted pickle. No scheduling,
checkpoint-boundary or exception-handler semantics are changed by record support.
