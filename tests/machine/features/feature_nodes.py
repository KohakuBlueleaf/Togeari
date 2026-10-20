"""Nodes of the machine feature checks; top level, so worker processes can import them."""

import time

from kohakunode import NodeLibrary

LIB = NodeLibrary("features")


@LIB.node()
def double(x: int) -> int:
    return x * 2


@LIB.node()
def nap(x: int, ms: int) -> int:
    time.sleep(ms / 1000.0)
    return x


@LIB.node()
def step(x: int) -> int:
    return x + 1
