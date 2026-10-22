"""Small portable record contracts and host nodes used by record integration tests."""

from kohakunode import NodeLibrary
from kohakunode.records import decode_record, encode_record, project

LIB = NodeLibrary("recordtest")
POINT = LIB.record("Point", [("x", int), ("y", int, 0), ("tags", "list[int]", [])])
ENVELOPE = LIB.record("Envelope", [("point", "Point"), ("labels", "dict[str, list[int]]", {})])
SCHEMAS = {schema.schema_id: schema for schema in LIB.records.values()}


@LIB.node()
def echo(record: "recordtest.Envelope") -> "recordtest.Envelope":  # noqa: F821
    return record


@LIB.node(mutates=["items"], outputs=["items"])
def push(items: list[int], value: int) -> list[int]:
    """A real in-place host mutation, unlike the functional kir.std.append node."""
    items.append(value)
    return items


def encode_request(inputs):
    return encode_record(inputs["record"])


def decode_response(payload):
    return decode_record(payload, SCHEMAS)


# A controlled host fault exercises snapshot/resume without timing assumptions.
FAIL = False
CALLS = []


@LIB.node(ordered=True)
def checkpoint(record: "recordtest.Point") -> int:  # noqa: F821
    x = project(POINT, record, "x")
    CALLS.append(x)
    if FAIL and x == 3:
        raise ValueError("stop at record x=3")
    return x
