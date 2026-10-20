"""Adapters: node inputs -> engine payload, engine reply -> node result."""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from kohakunode import Signature


@runtime_checkable
class Adapter(Protocol):
    # Engine protocols it can talk to.
    protocols: set[str]
    # Cheap enough to run on the dispatcher.
    light: bool

    def check(self, signature: Signature) -> list[str]:
        """Construction errors, if any."""

    def prepare(self, inputs: dict, protocol: str) -> Any: ...

    def decode(self, raw: Any, protocol: str) -> Any:
        """The node result, as the node function would return it."""


@dataclass
class PythonCall:
    """A Python node call: the function (pickled by reference) and its keyword inputs."""

    fn: Any
    kwargs: dict

    def run(self) -> Any:
        return self.fn(**self.kwargs)


class PythonAdapter:
    """Calls the node's library function with its inputs as keywords."""

    protocols = {"python"}
    light = True

    def __init__(self, fn: Any = None):
        self.fn = fn

    def bound(self, fn: Any) -> "PythonAdapter":
        return PythonAdapter(fn)

    def check(self, signature: Signature) -> list[str]:
        if self.fn is None:
            return [f"node {signature.name!r}: no Python implementation for PythonAdapter"]
        return []

    def prepare(self, inputs: dict, protocol: str) -> PythonCall:
        return PythonCall(self.fn, inputs)

    def decode(self, raw: Any, protocol: str) -> Any:
        return raw
