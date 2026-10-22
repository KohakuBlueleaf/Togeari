"""A small schema library and one simulated slow operation over immutable records."""

import time

from kohakunode import NodeLibrary
from kohakunode.records import project, replace_field

lib = NodeLibrary("geometry")
Point = lib.record("Point", [("x", int), ("y", int, 0), ("tags", "list[str]", [])])


@lib.node()
def translate(point: "Point", dx: int) -> "Point":
    """Stand in for a remote or compute-heavy operation; return a new value."""
    time.sleep(0.001)
    return replace_field(Point, point, "x", project(Point, point, "x") + dx)
