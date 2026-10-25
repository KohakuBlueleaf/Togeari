"""Log histograms of durations in microseconds: 64 bins, two per octave from 1 µs (spec/08 §8.5).

Bin 0 holds values below 1 µs. A value ``v >= 1`` with ``v = m * 2**e`` (``0.5 <= m < 1``)
falls in bin ``1 + 2 * (e - 1) + (m >= 0.75)``, clamped to 63: the lower half and the upper
half of each octave ``[2**k, 2**(k+1))``. Bin 63 is open above. Merging is elementwise
addition, so a histogram of merged windows equals the histogram of their values.
``togeari.plugins.telemetry.hist`` bins numpy arrays by the same rule.
"""

import math

BINS = 64


def bin_of(us: float) -> int:
    if not us >= 1.0:
        return 0
    m, e = math.frexp(us)
    return min(BINS - 1, 1 + 2 * (e - 1) + (m >= 0.75))


def lower(b: int) -> float:
    """The lower bound of bin ``b`` in µs."""
    if b <= 0:
        return 0.0
    k, half = divmod(b - 1, 2)
    return float(1 << k) * (1.5 if half else 1.0)


def upper(b: int) -> float:
    """The upper bound of bin ``b`` in µs (infinite for the last bin)."""
    return math.inf if b >= BINS - 1 else lower(b + 1)


def quantile(counts: list[int], p: float, lo: float, hi: float) -> float | None:
    """The ``p`` quantile of binned values, interpolated linearly inside its bin and kept
    within the observed ``[lo, hi]``; None when empty. Its error is at most the bin's width."""
    n = sum(counts)
    if n <= 0:
        return None
    rank = p * n
    seen = 0
    for b, c in enumerate(counts):
        if c and seen + c >= rank:
            a, z = max(lower(b), lo), min(upper(b), hi)
            v = a + (z - a) * ((rank - seen) / c)
            return min(max(v, lo), hi)
        seen += c
    return hi


class Hist:
    """Counts by bin, with the count, sum, minimum and maximum of the values added."""

    __slots__ = ("counts", "n", "total", "lo", "hi")

    def __init__(self) -> None:
        self.counts = [0] * BINS
        self.n = 0
        self.total = 0.0
        self.lo = math.inf
        self.hi = -math.inf

    def add(self, us: float) -> None:
        self.counts[bin_of(us)] += 1
        self.n += 1
        self.total += us
        self.lo = min(self.lo, us)
        self.hi = max(self.hi, us)

    def merge(self, other: "Hist") -> None:
        for b, c in enumerate(other.counts):
            self.counts[b] += c
        self.n += other.n
        self.total += other.total
        self.lo = min(self.lo, other.lo)
        self.hi = max(self.hi, other.hi)

    def quantile(self, p: float) -> float | None:
        return quantile(self.counts, p, self.lo, self.hi) if self.n else None
