"""The log histogram of ``plugins/common/hist.py`` over numpy arrays, and its stored forms."""

import numpy as np

from togeari.plugins.common.hist import BINS, quantile

__all__ = ["BINS", "bins", "dense", "edges", "quantile", "sparse"]


def bins(values: np.ndarray) -> np.ndarray:
    """The bin of each value (``plugins.common.hist.bin_of``, elementwise)."""
    v = np.asarray(values, dtype=np.float64)
    m, e = np.frexp(v)
    b = 1 + 2 * (e.astype(np.int64) - 1) + (m >= 0.75)
    return np.where(v >= 1.0, np.minimum(b, BINS - 1), 0)


def edges() -> np.ndarray:
    """The 65 bin edges in µs (the last one infinite)."""
    lo = [0.0] + [float(1 << ((b - 1) // 2)) * (1.5 if (b - 1) % 2 else 1.0) for b in range(1, 64)]
    return np.array([*lo, np.inf])


def sparse(row: np.ndarray) -> bytes:
    """One histogram as its first nonzero bin (a byte) and the u32 counts up to its last."""
    nz = np.flatnonzero(row)
    if not len(nz):
        return b""
    a, z = int(nz[0]), int(nz[-1])
    return bytes([a]) + np.minimum(row[a : z + 1], 0xFFFFFFFF).astype("<u4").tobytes()


def dense(blob: bytes) -> np.ndarray:
    """The 64 counts of a ``sparse`` histogram."""
    out = np.zeros(BINS, dtype=np.uint64)
    if blob:
        counts = np.frombuffer(blob, dtype="<u4", offset=1)
        out[blob[0] : blob[0] + len(counts)] = counts
    return out
