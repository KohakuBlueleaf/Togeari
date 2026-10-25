"""The log histogram: bins against a table, numpy against scalars, merges, quantile error."""

import math
import random

import pytest

np = pytest.importorskip("numpy")

from togeari.plugins.common.hist import BINS, Hist, bin_of, lower, upper  # noqa: E402
from togeari.plugins.telemetry.hist import bins, dense, edges, sparse  # noqa: E402

# (µs, bin): written from the definition, half octaves from 1 µs
TABLE = [
    (0.0, 0),
    (0.999, 0),
    (1.0, 1),
    (1.49, 1),
    (1.5, 2),
    (1.99, 2),
    (2.0, 3),
    (2.99, 3),
    (3.0, 4),
    (3.99, 4),
    (4.0, 5),
    (700.0, 19),  # 2**9 = 512 <= 700 < 1.5 * 512 = 768: bin 1 + 2 * 9
    (1000.0, 20),  # 768 <= 1000 < 1024
    (1024.0, 21),
    (2.0**31 * 1.5, 63),
    (2.0**40, 63),
    (float("nan"), 0),
    (-5.0, 0),
]


@pytest.mark.parametrize(("us", "b"), TABLE)
def test_bins_follow_the_table(us, b):
    assert bin_of(us) == b
    assert int(bins(np.array([us]))[0]) == b


def test_numpy_bins_equal_scalar_bins_and_the_edges():
    rng = random.Random(1)
    vals = [rng.lognormvariate(5, 3) for _ in range(20000)] + [2.0**k for k in range(40)]
    got = bins(np.array(vals))
    assert got.tolist() == [bin_of(v) for v in vals]
    e = edges()
    for v, b in zip(vals, got.tolist()):
        assert e[b] <= v < e[b + 1] or (b == 0 and v < 1)
        assert lower(b) == e[b] and (upper(b) == e[b + 1])


def test_merge_is_the_histogram_of_the_concatenation():
    rng = random.Random(2)
    a, b = Hist(), Hist()
    both = Hist()
    for i in range(3000):
        v = rng.expovariate(1 / 300)
        (a if i % 3 else b).add(v)
        both.add(v)
    a.merge(b)
    assert a.counts == both.counts and a.n == both.n
    assert (a.lo, a.hi) == (both.lo, both.hi)
    assert a.total == pytest.approx(both.total)


@pytest.mark.parametrize("p", [0.5, 0.9, 0.99])
def test_a_quantile_is_within_its_bin_of_the_exact_one(p):
    rng = random.Random(4)
    vals = sorted(rng.lognormvariate(6, 1.5) for _ in range(20001))
    h = Hist()
    for v in vals:
        h.add(v)
    exact = vals[int(p * (len(vals) - 1))]
    b = bin_of(exact)
    width = min(upper(b), h.hi) - lower(b)
    assert abs(h.quantile(p) - exact) <= width


def test_sparse_bytes_and_back():
    row = np.zeros(BINS, np.uint64)
    row[[5, 7]] = [3, 1]
    blob = sparse(row)
    assert blob == bytes([5]) + (3).to_bytes(4, "little") + bytes(4) + (1).to_bytes(4, "little")
    assert dense(blob).tolist() == row.tolist()
    assert sparse(np.zeros(BINS, np.uint64)) == b"" and not dense(b"").any()
    assert math.isinf(upper(BINS - 1))
