"""Transport payloads of a given size and kind.

- ``ndarray``: a uint8 array (out of band under pickle protocol 5, so shared memory applies);
- ``bytes``: a bytes object (always in band);
- ``pyobj``: a dict of short strings and ints (many small objects to pickle).
"""

import numpy as np

KINDS = ("ndarray", "bytes", "pyobj")


def make(kind: str, size: int, seed: int = 0) -> object:
    rng = np.random.default_rng(seed)
    match kind:
        case "ndarray":
            return rng.integers(0, 256, size, dtype=np.uint8)
        case "bytes":
            return rng.integers(0, 256, size, dtype=np.uint8).tobytes()
        case "pyobj":
            # About 32 pickled bytes per entry
            n = max(1, size // 32)
            return {f"k{i:08d}": [i, f"v{i:06d}"] for i in range(n)}
        case _:
            raise ValueError(f"payload kind {kind!r}")
