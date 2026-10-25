"""Rollups: records into 1 s rows, and rows merged into coarser buckets (spec/08 §8.5).

A coarser tier is always merged from the finer one's rows (counts and histograms add, minima
and maxima take the extreme), never recomputed from records, so every tier agrees with the raw
records exactly in counts and sums.
"""

from dataclasses import dataclass

import numpy as np

from togeari.core.contracts import telemetry as T
from togeari.plugins.telemetry import layout as L
from togeari.plugins.telemetry.hist import BINS, bins


@dataclass
class Rows:
    """Rollup rows as columns; ``h`` is (rows, 64) bin counts. ``lo`` / ``hi`` are +inf / -inf
    for a row without values."""

    t: np.ndarray
    ent: np.ndarray
    met: np.ndarray
    n: np.ndarray
    sum: np.ndarray
    lo: np.ndarray
    hi: np.ndarray
    h: np.ndarray

    @staticmethod
    def empty() -> "Rows":
        return Rows(
            np.zeros(0, np.int64),
            np.zeros(0, np.uint64),
            np.zeros(0, np.uint8),
            np.zeros(0, np.uint64),
            np.zeros(0, np.float64),
            np.zeros(0, np.float64),
            np.zeros(0, np.float64),
            np.zeros((0, BINS), np.uint64),
        )

    def __len__(self) -> int:
        return len(self.t)

    def take(self, idx: np.ndarray) -> "Rows":
        return Rows(*(getattr(self, f)[idx] for f in FIELDS))


FIELDS = ("t", "ent", "met", "n", "sum", "lo", "hi", "h")


def concat(*parts: Rows) -> Rows:
    parts = [p for p in parts if len(p)]
    if not parts:
        return Rows.empty()
    return Rows(*(np.concatenate([getattr(p, f) for p in parts]) for f in FIELDS))


def _groups(t: np.ndarray, ent: np.ndarray, met: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(the sort order by (t, ent, met), the start of each group in it)."""
    order = np.lexsort((met, ent, t))
    if not len(order):
        return order, order
    ts, es, ms = t[order], ent[order], met[order]
    new = np.ones(len(order), dtype=bool)
    new[1:] = (ts[1:] != ts[:-1]) | (es[1:] != es[:-1]) | (ms[1:] != ms[:-1])
    return order, np.flatnonzero(new)


def reduce(rows: Rows) -> Rows:
    """One row per (t, ent, met)."""
    if not len(rows):
        return rows
    order, starts = _groups(rows.t, rows.ent, rows.met)
    r = rows.take(order)
    return Rows(
        r.t[starts],
        r.ent[starts],
        r.met[starts],
        np.add.reduceat(r.n, starts),
        np.add.reduceat(r.sum, starts),
        np.minimum.reduceat(r.lo, starts),
        np.maximum.reduceat(r.hi, starts),
        np.add.reduceat(r.h, starts, axis=0),
    )


def observe(recs: np.ndarray, epoch_wall: float) -> Rows:
    """The 1 s rows of a batch of records (``layout.REC``)."""
    if not len(recs):
        return Rows.empty()
    sec = np.floor(epoch_wall + recs["t_ns"] / 1e9).astype(np.int64)
    kind = recs["kind"]
    status = recs["status"]
    flags = recs["flags"]
    qv = (flags & T.QUEUED) != 0
    sv = (flags & T.SERVICE) != 0
    q = recs["queued_us"].astype(np.float64)
    s = recs["service_us"].astype(np.float64)
    aux = recs["aux"].astype(np.float64)
    addr = recs["addr"].astype(np.uint64)
    node = np.uint64(L.NODE << 56) | addr
    entry = np.uint64(L.ENTRY << 56) | addr
    lane = np.uint64(L.LANE << 56) | recs["lane"].astype(np.uint64)
    unit = np.uint64(L.UNIT << 56) | recs["unit"].astype(np.uint64)
    has_lane = recs["lane"] != 0
    has_unit = recs["unit"] != 0
    has_addr = recs["addr"] != T.NO_ADDR

    # Observations: (mask, entity, metric, values or None)
    calls = np.isin(kind, (T.ENQUEUE, T.INLINE, T.HIT)) & has_addr
    result = kind == T.RESULT
    inline = kind == T.INLINE
    submit = kind == T.SUBMIT
    end = kind == T.END
    cancelled = result & (status == T.RESULT_STATUS["Cancelled"])
    failed_result = result & (status != 0) & ~cancelled
    failed_call = failed_result | (inline & (status != 0))
    obs = [
        (calls, node, L.CALLS, None),
        (calls & qv, node, L.WAIT, q),
        (result & qv & has_addr, node, L.QUEUE, q),
        (result & sv & has_addr, node, L.SERVICE, s),
        (inline & sv, node, L.SERVICE, s),
        (result & qv & has_lane, lane, L.QUEUE, q),
        (result & sv & has_lane, lane, L.SERVICE, s),
        (result & qv & has_unit, unit, L.QUEUE, q),
        (result & sv & has_unit, unit, L.SERVICE, s),
        (failed_call & has_addr, node, L.ERRORS, None),
        (failed_result & has_unit, unit, L.ERRORS, None),
        ((kind == T.FAULT) & has_addr, node, L.FAULTS, None),
        (cancelled & has_addr, node, L.CANCELLED, None),
        (cancelled & has_unit, unit, L.CANCELLED, None),
        (submit & has_lane, lane, L.BATCH, aux),
        (submit & has_unit, unit, L.BATCH, aux),
        (end & sv, entry, L.JOB, s),
        (end & (status != 0), entry, L.FAILED, None),
        ((kind == T.ADMIT) & has_addr, entry, L.ADMITTED, None),
    ]
    ts, es, ms, vs = [], [], [], []
    for mask, ent, met, val in obs:
        idx = np.flatnonzero(mask)
        if not len(idx):
            continue
        ts.append(sec[idx])
        es.append(ent[idx])
        ms.append(np.full(len(idx), met, np.uint8))
        vs.append(np.full(len(idx), np.nan) if val is None else val[idx])
    if not ts:
        return Rows.empty()
    t, ent, met, val = (np.concatenate(x) for x in (ts, es, ms, vs))

    # One row per (second, entity, metric)
    order, starts = _groups(t, ent, met)
    t, ent, met, val = t[order], ent[order], met[order], val[order]
    valued = ~np.isnan(val)
    v0 = np.where(valued, val, 0.0)
    first = np.zeros(len(t), dtype=bool)
    first[starts] = True
    gi = np.cumsum(first) - 1
    g = len(starts)
    h = np.bincount(gi[valued] * BINS + bins(val[valued]), minlength=g * BINS)
    return Rows(
        t[starts],
        ent[starts],
        met[starts],
        np.add.reduceat(np.ones(len(t), np.uint64), starts),
        np.add.reduceat(v0, starts),
        np.minimum.reduceat(np.where(valued, val, np.inf), starts),
        np.maximum.reduceat(np.where(valued, val, -np.inf), starts),
        h.astype(np.uint64).reshape(g, BINS),
    )


class Tier:
    """Open buckets of one period: rows added are re-bucketed and merged; ``close`` hands out
    the buckets that end at or before a time."""

    def __init__(self, period: int):
        self.period = period
        self.open = Rows.empty()

    def add(self, rows: Rows) -> None:
        if not len(rows):
            return
        b = Rows(*(getattr(rows, f) for f in FIELDS))
        b.t = rows.t - rows.t % self.period
        self.open = reduce(concat(self.open, b))

    def close(self, before: float) -> Rows:
        """The buckets ending at or before ``before`` (unix seconds); they leave the tier."""
        done = self.open.t + self.period <= before
        out = self.open.take(np.flatnonzero(done))
        self.open = self.open.take(np.flatnonzero(~done))
        return out
