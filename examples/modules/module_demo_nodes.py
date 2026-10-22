"""Small, deterministic host implementations; no services or optional dependencies."""

from kohakunode import NodeLibrary

LIB = NodeLibrary("module_demo")


@LIB.node(outputs=["accepted"])
def keep(value: int) -> bool:
    return value >= 0


@LIB.node(outputs=["text"])
def label(prefix: str, value: int) -> str:
    return f"{prefix}:{value}"
