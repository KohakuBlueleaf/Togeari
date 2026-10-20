"""The ``TorchAdapter`` (protocol ``"torch"``): tensors in, tensors out."""

from typing import Any

from kohakunode import Signature


class TorchAdapter:
    """The payload is the node's inputs dict, or the single input named ``arg``; the reply is
    the result.
    """

    protocols = {"torch"}
    light = True

    def __init__(self, arg: str | None = None):
        self.arg = arg

    def check(self, signature: Signature) -> list[str]:
        names = [p.name for p in signature.inputs]
        if self.arg is not None and self.arg not in names:
            return [f"node {signature.name!r}: TorchAdapter input {self.arg!r} not in {names}"]
        return []

    def prepare(self, inputs: dict, protocol: str) -> Any:
        return inputs if self.arg is None else inputs[self.arg]

    def decode(self, raw: Any, protocol: str) -> Any:
        return raw
