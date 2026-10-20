"""Plain values of the machine for plugins: JSON-friendly views of objects and values."""

import json
from typing import Any

PRIMITIVES = (int, float, str, bool, type(None))


def plain(v: Any) -> Any:
    """A JSON-friendly view of a value: primitives as is, named objects by name, else the type."""
    match v:
        case _ if isinstance(v, PRIMITIVES):
            return v
        case list() | tuple():
            return [plain(x) for x in v]
        case dict():
            return {str(k): plain(x) for k, x in v.items()}
        case _ if isinstance(getattr(v, "name", None), str):
            return {"type": type(v).__name__, "name": v.name}
        case _:
            return type(v).__name__


def describe(obj: Any) -> Any:
    """An object's type with its primitive attributes (policies, adapters, stores)."""
    if obj is None or isinstance(obj, PRIMITIVES):
        return obj
    attrs = getattr(obj, "__dict__", {})
    return {
        "type": type(obj).__name__,
        **{k: v for k, v in attrs.items() if not k.startswith("_") and isinstance(v, PRIMITIVES)},
    }


def json_default(v: Any) -> Any:
    match v:
        case bytes() | bytearray():
            return bytes(v).hex()
        case set() | frozenset():
            return sorted(v, key=repr)
        case _:
            return repr(v)


def dumps(v: Any, **kw: Any) -> str:
    return json.dumps(v, default=json_default, **kw)
