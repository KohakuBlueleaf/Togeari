"""Node functions for ``PythonCallEngine`` (top-level, so worker processes import them by name).

- ``spin``: pure Python arithmetic, ``n`` iterations; holds the GIL throughout;
- ``digest``: SHA-256 over ``n`` bytes of a module buffer; releases the GIL while hashing;
- ``checksum``: a strided sum over an array input; the input's size is the transport cost.
"""

import hashlib

import numpy as np
from kohakunode import NodeLibrary

lib = NodeLibrary("s1py")

# The hashed buffer, allocated once per process at import
BUFFER = bytes(64 << 20)


@lib.node()
def spin(x: int, n: int) -> int:
    acc = x
    for i in range(n):
        acc = (acc * 31 + i) % 1_000_003
    return acc


@lib.node()
def digest(x: int, n: int) -> str:
    view = memoryview(BUFFER)[: min(n, len(BUFFER))]
    return hashlib.sha256(view).hexdigest()


@lib.node()
def checksum(x: int, blob: np.ndarray) -> int:
    return int(blob[::64].sum()) + x


INPUTS = {
    "spin": ("x", "n"),
    "digest": ("x", "n"),
    "checksum": ("x", "blob"),
}
