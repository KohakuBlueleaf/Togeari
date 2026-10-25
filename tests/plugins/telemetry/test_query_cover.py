"""Range queries over tiers in every state: closed hours and minutes, the open ones, and 1 s
files deleted by retention.

The reference is the raw records themselves: a range ``[a, b)`` of whole buckets must count
exactly the records whose second falls in it, whichever tiers the query reads.
"""

import json
import math

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("kohakuvault")

from kohakuvault import ColumnVault  # noqa: E402

from togeari.core.contracts import telemetry as R  # noqa: E402
from togeari.plugins.telemetry import layout as L  # noqa: E402
from togeari.plugins.telemetry.query import Run  # noqa: E402
from togeari.plugins.telemetry.rollup import Tier, observe  # noqa: E402
from togeari.plugins.telemetry.store import Segments, files_of, remove  # noqa: E402
from togeari.plugins.telemetry.writer import Writer  # noqa: E402

# 2.5 hours from an hour boundary, so the last hour and minute stay open
T0 = 1_700_002_800
SECONDS = 9000
ADDRS = (3, 7, 11)


def records(rng) -> np.ndarray:
    n = 30_000
    recs = np.zeros(n, dtype=L.REC)
    recs["t_ns"] = np.sort(rng.integers(0, SECONDS * 10**9, n)).astype(np.uint64)
    recs["kind"] = R.RESULT
    recs["addr"] = rng.choice(ADDRS, n)
    recs["service_us"] = rng.lognormal(7, 1.5, n).astype(np.uint32)
    recs["flags"] = R.SERVICE
    return recs


def write_tiers(root, recs) -> int:
    """Tick every 10 s as the writer does; the time up to which the 1 s tier is closed."""
    tiers = [
        (Tier(period), Segments(root, name, {**L.ROLLUP_COLUMNS, "h": L.hist_dtype(name)}, seg))
        for name, period, seg in L.TIERS
    ]
    sec = T0 + recs["t_ns"] // 10**9
    cutoff = T0
    for now in range(T0 + 10, T0 + SECONDS, 10):
        part = recs[(sec >= now - 10) & (sec < now)]
        tiers[0][0].add(observe(part, float(T0)))
        cutoff = now - 1
        carry = None
        for tier, seg in tiers:
            if carry is not None:
                tier.add(carry)
            carry = tier.close(cutoff)
            if len(carry):
                Writer._write_rows(None, seg, carry)
    for _, seg in tiers:
        seg.close()
    return cutoff


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    root = tmp_path_factory.mktemp("cover")
    rng = np.random.default_rng(5)
    recs = records(rng)
    closed = write_tiers(root, recs)

    # Retention took the first hour's 1 s file
    first = files_of(root, "r1s", 3600)[0][1]
    assert remove(first)
    return Run(root), recs, closed


def truth(recs, a, b, addr=None):
    sec = T0 + recs["t_ns"] // 10**9
    keep = (sec >= a) & (sec < b)
    if addr is not None:
        keep &= recs["addr"] == addr
    return recs[keep]


def ranges(closed):
    # Whole minutes in the deleted hour (only 1 min and 1 h rows remain there), seconds
    # elsewhere: across the closed hours, into the open hour, inside one minute
    yield T0, closed
    yield T0 + 120, T0 + 3600 + 37
    yield T0 + 3600 + 5, T0 + 7200 + 61
    yield T0 + 3600, T0 + 7200
    yield T0 + 7200 + 13, closed
    yield T0 + 5000, T0 + 5007
    yield T0 + 600, T0 + 7800 + 3


@pytest.mark.parametrize("i", range(7))
def test_a_range_counts_every_record_once(run, i):
    r, recs, closed = run
    a, b = list(ranges(closed))[i]
    for addr in ADDRS:
        want = truth(recs, a, b, addr)
        e = L.entity(L.NODE, addr)
        h = r.hist(e, "service", a, b)
        assert h["n"] == len(want)
        assert h["sum"] == pytest.approx(float(want["service_us"].sum()))
        if len(want):
            assert h["max"] == float(want["service_us"].max())
    top = {t["id"]: t["n"] for t in r.top("node", "service", a, b, "count", 10)}
    assert top == {x: len(truth(recs, a, b, x)) for x in ADDRS if len(truth(recs, a, b, x))}
    table = {t["id"]: t["metrics"]["service"]["n"] for t in r.table("node", a, b)}
    assert table == top


def test_the_cover_reads_coarse_tiers_inside_and_fine_tiers_at_the_edges(run):
    r, _, closed = run
    assert r.cover(T0 + 3605, T0 + 7261) == [
        ("r1s", T0 + 3605, T0 + 3660),
        ("r1m", T0 + 3660, T0 + 7260),
        ("r1s", T0 + 7260, T0 + 7261),
    ]

    # The closed hours from r1h, the open hour's closed minutes from r1m, its last seconds
    # from r1s
    last_minute = r.extent("r1m")[1]
    assert r.extent("r1h")[1] == T0 + 7200
    assert r.cover(T0, closed) == [
        ("r1h", T0, T0 + 7200),
        ("r1m", T0 + 7200, last_minute),
        ("r1s", last_minute, closed),
    ]

    # Fine first: r1s from where retention left it, r1m before
    assert r.cover(T0 + 120, closed, fine=True) == [
        ("r1m", T0 + 120, T0 + 3600),
        ("r1s", T0 + 3600, closed),
    ]


def test_a_series_sums_to_the_range_count(run):
    r, recs, closed = run
    a, b = T0 + 120, closed
    e = L.entity(L.NODE, ADDRS[0])
    for points, tier in ((50, "r1m"), (500, "r1s"), (5000, "r1s")):
        s = r.series(e, "service", a, b, points, "count")
        assert s["tier"] == tier
        end = min(b, r.extent(tier)[1])
        assert sum(v or 0 for v in s["v"]) == len(truth(recs, a, end, ADDRS[0])), points
        assert all(a <= t < b for t in s["t"])

        # The deleted hour is read from r1m, so no bucket is finer than a minute
        assert s["step"] >= max(60, math.floor((b - a) / points))


# A 10 min run that starts 1957 s into an hour, with nothing deleted
START, STOP = T0 + 1957.3, T0 + 2557.9


@pytest.fixture(scope="module")
def short(tmp_path_factory):
    root = tmp_path_factory.mktemp("short")
    recs = records(np.random.default_rng(6))
    sec = T0 + recs["t_ns"] // 10**9
    recs = recs[(sec >= math.floor(START)) & (sec < math.floor(STOP))]
    write_tiers(root, recs)
    (root / L.MACHINE_FILE).write_text(json.dumps({"started": START, "stopped": STOP}))
    return Run(root), recs


def test_a_file_read_while_the_writer_appends_answers_its_complete_rows(tmp_path):
    # The writer appends one column after another: a reader can find the first columns longer
    # than the rest. Those extra rows are not answered, and the complete ones are
    recs = records(np.random.default_rng(6))
    sec = T0 + recs["t_ns"] // 10**9
    recs = recs[(sec >= math.floor(START)) & (sec < math.floor(STOP))]
    write_tiers(tmp_path, recs)
    (tmp_path / L.MACHINE_FILE).write_text(json.dumps({"started": START, "stopped": STOP}))
    r = Run(tmp_path)
    a, b = r.span()
    e = L.entity(L.NODE, ADDRS[0])
    before = r.series(e, "service", a, b, 601, "count")

    _, path = files_of(tmp_path, "r1s", 3600)[-1]
    v = ColumnVault(str(path))
    # Five more rows of the queried entity in the leading columns (t, ent, met) only
    cols = {n: v.ensure(n, L.ROLLUP_COLUMNS[n]) for n in list(L.ROLLUP_COLUMNS)[:3]}
    row = np.flatnonzero(cols["ent"].read_array() == e)[-1]
    for col in cols.values():
        col.append_array(np.repeat(col.read_array()[row : row + 1], 5))
    v.checkpoint()
    v.close()

    got = r.series(e, "service", a, b, 601, "count")
    assert got == before
    assert sum(v or 0 for v in got["v"]) == len(truth(recs, a, b, ADDRS[0]))
    assert r.hist(e, "service", a, b)["n"] == len(truth(recs, a, b, ADDRS[0]))


def test_the_span_is_the_run_not_the_hour_holding_it(short):
    r, _ = short
    assert r.extent("r1h") == (T0, T0 + 3600)
    assert r.span() == (math.floor(START), math.ceil(STOP))


def test_a_run_starting_inside_an_hour_is_read_from_its_finest_tier(short):
    r, recs = short
    a, b = r.span()

    # The hour and minute holding the start are not data retention deleted
    assert r.cover(T0, T0 + 3600, fine=True) == [
        ("r1m", T0, T0 + 1920),
        ("r1s", T0 + 1920, T0 + 3600),
    ]
    e = L.entity(L.NODE, ADDRS[0])
    for t0, t1, points, step in ((a, b, 601, 1), (a, b, 300, 3), (T0, T0 + 3600, 3600, 1)):
        s = r.series(e, "service", t0, t1, points, "count")
        assert (s["tier"], s["step"]) == ("r1s", step)
        assert sum(v or 0 for v in s["v"]) == len(truth(recs, t0, t1, ADDRS[0]))
    h = r.hist(e, "service", T0, T0 + 3600)
    assert h["n"] == len(truth(recs, a, b, ADDRS[0]))

    # Replies are strict JSON: the last bin has no upper edge rather than an infinite one
    assert h["edges"][-1] is None
    json.dumps(h, allow_nan=False)
