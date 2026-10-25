"""The telemetry ring: record bytes against a hand-built layout, both rings alike, drops."""

import hashlib
import random

import pytest

from togeari.core.contracts import telemetry as R
from togeari.core.observe.ring import Ring

_rs = pytest.importorskip("togeari._rs")


def by_hand(t, kind, job, addr, lane, unit, q, s, aux, status, flags) -> bytes:
    """The layout written field by field, without ``struct``."""
    le = lambda v, n: int(v).to_bytes(n, "little")  # noqa: E731
    return b"".join(
        [
            le(t, 8),
            le(job, 8),
            le(addr, 4),
            le(q, 4),
            le(s, 4),
            le(aux, 4),
            le(lane, 2),
            le(unit, 2),
            le(kind, 1),
            le(status, 1),
            le(flags, 2),
        ]
    )


CASES = [
    # (pack args), (expected q, s, addr, flags)
    ((5, R.RESULT, 7, 12, 1, 2, 0.0015, 0.25, 3, 1), (1500, 250000, 12, R.QUEUED | R.SERVICE)),
    ((0, R.ADMIT, 1, None, 0, 0, None, None, 0, 0), (0, 0, R.NO_ADDR, 0)),
    ((9, R.END, 2, -4, 0, 0, None, 1e6, 0, 2), (0, R.U32, R.NO_ADDR, R.SERVICE | R.SERVICE_SAT)),
    ((1, R.ENQUEUE, 3, 9, 4, 0, -2.0, None, 0, 0), (0, 0, 9, R.QUEUED)),
    (
        (2, R.SUBMIT, 2**64 - 1, 2**32 - 2, 65535, 65535, None, None, 2**40 + 5, 0),
        (0, 0, 2**32 - 2, 0),
    ),
]


@pytest.mark.parametrize(("args", "want"), CASES)
def test_a_record_is_the_documented_layout(args, want):
    t, kind, job, addr, lane, unit, q_s, s_s, aux, status = args
    q, s, a, flags = want
    expected = by_hand(t, kind, job, a, lane, unit, q, s, aux & R.U32, status, flags)
    assert R.pack(*args) == expected
    assert _rs.TelemetryRing.pack(*args) == expected
    assert len(expected) == R.SIZE == _rs.TELEMETRY_RECORD_BYTES


def test_both_packs_agree_on_random_fields():
    rng = random.Random(3)
    digest_py, digest_rs = hashlib.sha256(), hashlib.sha256()
    for _ in range(5000):
        secs = lambda: rng.choice([None, rng.uniform(-1, 5000), rng.expovariate(100)])  # noqa: E731
        args = (
            rng.getrandbits(63),
            rng.randrange(1, 11),
            rng.getrandbits(64),
            rng.choice([None, -1, rng.getrandbits(32), rng.getrandbits(40)]),
            rng.getrandbits(16),
            rng.getrandbits(16),
            secs(),
            secs(),
            rng.getrandbits(40),
            rng.randrange(0, 5),
        )
        digest_py.update(R.pack(*args))
        digest_rs.update(_rs.TelemetryRing.pack(*args))
    assert digest_py.hexdigest() == digest_rs.hexdigest()


@pytest.mark.parametrize("make", [Ring, _rs.TelemetryRing], ids=["python", "native"])
def test_a_full_ring_drops_counts_and_keeps_order(make):
    r = make(4)
    assert r.capacity == 4
    for j in range(6):
        r.put(R.ADMIT, j, 1, "lane-a", None, None, None, 0, 0)
    c = r.counts()
    assert (c["written"], c["dropped"], c["pending"]) == (4, 2, 4)
    data = r.drain()
    assert [data[i * R.SIZE + 8] for i in range(4)] == [0, 1, 2, 3]

    # After a drain the order carries on across the wrap; drain(max) takes the oldest first
    for j in range(10, 13):
        r.put(R.END, j, None, None, "unit-b", None, 0.5, 0, 1)
    first = r.drain(2)
    rest = r.drain()
    assert [first[8], first[R.SIZE + 8], rest[8]] == [10, 11, 12]
    assert rest[32:36] == bytes([0, 0, 1, 0])  # no lane; unit id 1


@pytest.mark.parametrize("make", [Ring, _rs.TelemetryRing], ids=["python", "native"])
def test_names_keep_their_ids(make):
    r = make(16)
    assert r.name_id("lane", "a") == 1
    assert r.name_id("unit", "a") == 1
    assert r.name_id("lane", "b") == 2
    r.leave("lane", "a")
    assert r.name_id("lane", "a") == 1
    rows = {(k, i): (n, left) for k, i, n, _, left in r.name_rows()}
    assert rows[("lane", 1)][0] == "a" and rows[("lane", 1)][1] is not None
    assert rows[("lane", 2)] == ("b", None)
