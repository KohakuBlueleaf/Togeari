"""Pure-Python signal features: the CPU-heavy step, run in worker processes."""

import cmath
import math

BINS = 16


def features(window: dict) -> list:
    """Log magnitudes of the first ``BINS`` frequency bins of the window's samples."""
    samples = window["samples"]
    n = len(samples)
    out = []
    for k in range(1, BINS + 1):
        step = -2j * math.pi * k / n
        acc = sum(x * cmath.exp(step * t) for t, x in enumerate(samples))
        out.append(math.log1p(abs(acc) / n))
    return out
