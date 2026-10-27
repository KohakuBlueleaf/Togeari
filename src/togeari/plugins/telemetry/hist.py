"""The log histogram of ``plugins/common/hist.py`` over numpy arrays, and its stored forms."""

import numpy as np

from togeari.plugins.common.hist import BINS, quantile

__all__ = ["BINS", "bins", "dense", "dense_rows", "edges", "quantile", "sparse"]


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


def dense_rows(payload, offsets: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """The ``(len(idx), 64)`` counts of the ``sparse`` histograms ``idx`` of a column's
    buffers (``payload`` the bytes, blob ``i`` at ``offsets[i]:offsets[i + 1]``)."""
    raw = np.frombuffer(payload, np.uint8)
    off = np.asarray(offsets, np.int64)
    a, z = off[idx], off[np.asarray(idx) + 1]
    k = np.where(z > a, (z - a - 1) // 4, 0)
    out = np.zeros((len(idx), BINS), np.uint64)
    if not k.sum():
        return out
    row = np.repeat(np.arange(len(idx)), k)
    j = np.arange(int(k.sum())) - np.repeat(np.cumsum(k) - k, k)
    at = a[row] + 1 + 4 * j
    vals = raw[at].astype(np.uint64)
    for s in (1, 2, 3):
        vals |= raw[at + s].astype(np.uint64) << np.uint64(8 * s)
    out[row, raw[a[row]].astype(np.int64) + j] = vals
    return out
