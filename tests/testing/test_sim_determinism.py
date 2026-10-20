"""Simulated engine delays: fixed per seed, different across seeds, shaped by the input."""

import hashlib

from togeari.testing.sim import SimEngine

# Fingerprint of the first 32 delays of SimEngine(seed=0, latency_ms=2.0) over inputs 0..31
SEED0_DELAYS = "942ff4c8658e70f981f75d24ab1dda809e7edaac4d768df67452a56ad2483e49"


class Item:
    node = "f"

    def __init__(self, x):
        self.x = x

    def digest(self) -> bytes:
        return str(self.x).encode()


def delays(engine: SimEngine, xs) -> list[float]:
    return [engine.delay(Item(x)) for x in xs]


def fingerprint(values: list[float]) -> str:
    return hashlib.sha256(repr([round(d, 12) for d in values]).encode()).hexdigest()


def test_one_seed_gives_the_pinned_delays():
    assert fingerprint(delays(SimEngine(seed=0, latency_ms=2.0), range(32))) == SEED0_DELAYS


def test_another_seed_gives_other_delays():
    a = delays(SimEngine(seed=0, latency_ms=2.0), range(32))
    b = delays(SimEngine(seed=1, latency_ms=2.0), range(32))
    assert a != b


def test_the_input_sets_the_tail():
    # Same seed, so the same jitter draws; only the inputs differ
    base = delays(SimEngine(seed=0, latency_ms=2.0), range(32))
    other = delays(SimEngine(seed=0, latency_ms=2.0), range(100, 132))
    assert base != other

    # Without a tail the input does not matter
    flat = delays(SimEngine(seed=0, latency_ms=2.0, tail=0.0), range(32))
    flat_other = delays(SimEngine(seed=0, latency_ms=2.0, tail=0.0), range(100, 132))
    assert flat == flat_other

    # The tail only lengthens: each delay is at least its flat counterpart
    assert all(d >= f for d, f in zip(base, flat, strict=True))
    assert any(d > f for d, f in zip(base, flat, strict=True))


def test_zero_latency_is_no_delay():
    assert delays(SimEngine(seed=0, latency_ms=0.0), range(8)) == [0.0] * 8
