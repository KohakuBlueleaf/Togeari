"""Reading a telemetry directory: series, rankings, histograms, raw records, traces, programs.

Every call opens the files it reads read-only and closes them before it returns, so a reader
holds no snapshot between calls and never blocks the writer. A range is read as a cover
(``Run.cover``) that reads each bucket once: totals from the coarsest tier that holds a
bucket whole, a series from the tier its point budget fits, coarser only where retention
deleted it.
"""

import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np
from kohakuvault import ColumnVault, KVault

from togeari.core.contracts import telemetry as T
from togeari.plugins.telemetry import layout as L
from togeari.plugins.telemetry.hist import BINS, dense, edges, quantile
from togeari.plugins.telemetry.rollup import Rows, concat
from togeari.plugins.telemetry.store import files_of

STATS = ("count", "rate", "mean", "min", "max", "p50", "p90", "p99", "sum")
TIER = {name: (period, seg) for name, period, seg in L.TIERS}


def _vault(path: Path) -> ColumnVault:
    return ColumnVault(str(path), read_only=True)


def _has(v: ColumnVault, column: str) -> bool:
    return any(c[0] == column for c in v.list_columns())


def complete(cols: dict[str, np.ndarray], *lengths: int) -> dict[str, np.ndarray]:
    """The rows every column holds: the writer appends one column after another, so a file
    read while it writes can hold more of the first columns than of the last."""
    n = min([len(a) for a in cols.values()] + list(lengths))
    return {k: a[:n] for k, a in cols.items()}


def record_dict(r: np.void, epoch: float) -> dict:
    """One record as a dict: wall time ``t``, the kind's name, None for absent fields."""
    return {
        "t": epoch + int(r["t_ns"]) / 1e9,
        "job": int(r["job"]),
        "kind": T.NAMES.get(int(r["kind"]), str(int(r["kind"]))),
        "addr": None if int(r["addr"]) == T.NO_ADDR else int(r["addr"]),
        "lane": int(r["lane"]),
        "unit": int(r["unit"]),
        "queued_us": int(r["queued_us"]) if r["flags"] & T.QUEUED else None,
        "service_us": int(r["service_us"]) if r["flags"] & T.SERVICE else None,
        "aux": int(r["aux"]),
        "status": int(r["status"]),
    }


def _stat(stat: str, n: float, s: float, lo: float, hi: float, h: np.ndarray, step: float):
    match stat:
        case "count":
            return float(n)
        case "rate":
            return n / step
        case "sum":
            return s
        case "mean":
            return s / n if n else None
        case "min":
            return lo if np.isfinite(lo) else None
        case "max":
            return hi if np.isfinite(hi) else None
        case "p50" | "p90" | "p99":
            if not h.sum():
                return None
            return quantile(h.tolist(), int(stat[1:]) / 100, lo, hi)
    raise ValueError(f"stat must be one of {STATS}, not {stat!r}")


class Run:
    """One telemetry directory (``Telemetry(dir=...)``)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ------------------------------------------------------------ the run
    def info(self) -> dict:
        return json.loads((self.path / L.MACHINE_FILE).read_text(encoding="utf-8"))

    def _kv(self, name: str) -> KVault:
        return KVault(str(self.path / name), read_only=True)

    def static(self, key: str, default: Any = None) -> Any:
        p = self.path / L.STATIC_FILE
        if not p.exists():
            return default
        with self._kv(L.STATIC_FILE) as kv:
            return kv.get(key, default)

    def names(self) -> list[dict]:
        rows = self.static("names", []) or []
        return [
            {"kind": k, "id": i, "name": n, "appeared_ns": a, "left_ns": z}
            for k, i, n, a, z in rows
        ]

    def programs(self) -> list[dict]:
        """Every program without its graphs and statement table."""
        with self._kv(L.STATIC_FILE) as kv:
            return [
                {k: v[k] for k in v if k not in ("graphs", "statements")}
                for v in kv.values(prefix="program/")
            ]

    def program(self, start: int) -> dict | None:
        return self.static(f"program/{start:012d}")

    def series_names(self) -> dict[int, dict]:
        with self._kv(L.STATIC_FILE) as kv:
            return {int(k.decode().split("/")[1]): v for k, v in kv.items(prefix="series/")}

    def span(self) -> tuple[float, float] | None:
        """The start of the first bucket and the end of the last one in any tier, inside the
        run's own time (its start to its stop, or to now while it runs)."""
        ext = [e for e in map(self.extent, TIER) if e is not None]
        if not ext:
            return None
        info = self.info()
        lo = max(min(e[0] for e in ext), math.floor(info.get("started") or 0))
        hi = min(max(e[1] for e in ext), math.ceil(info.get("stopped") or time.time()))
        return (lo, max(lo, hi))

    # ------------------------------------------------------------ rollups
    def rows(
        self,
        tier: str,
        t0: float,
        t1: float,
        ents: list[int] | None = None,
        mets: list[int] | None = None,
    ) -> Rows:
        """The rows of ``tier`` with bucket start in ``[t0, t1)``, of those entities and
        metrics (None: all)."""
        period, seg = TIER[tier]
        parts = []
        for start, p in files_of(self.path, tier, seg):
            if seg is not None and (start + seg <= t0 - period or start >= t1):
                continue
            with _vault(p) as v:
                if not _has(v, "t"):
                    continue
                full = {n: v[n].read_array() for n in ("t", "ent", "met", "n", "sum", "lo", "hi")}
                if tier == "r1s":
                    payload, offsets = v["h"].read_buffers()
                    hn = len(offsets) - 1
                else:
                    hs = v["h"].read_array()
                    hn = len(hs)
                full = complete(full, hn)
                t = full["t"]
                keep = (t >= t0) & (t < t1)
                if ents is not None:
                    keep &= np.isin(full["ent"], np.asarray(ents, np.uint64))
                if mets is not None:
                    keep &= np.isin(full["met"], np.asarray(mets, np.uint8))
                idx = np.flatnonzero(keep)
                if not len(idx):
                    continue
                cols = {n: a[idx] for n, a in full.items()}
                if tier == "r1s":
                    h = np.stack(
                        [dense(payload[int(offsets[i]) : int(offsets[i + 1])]) for i in idx]
                    )
                else:
                    h = hs[idx].astype(np.uint64)
            parts.append(Rows(**cols, h=h.reshape(len(idx), BINS)))
        return concat(*parts)

    def extent(self, tier: str) -> tuple[float, float] | None:
        """The start of a tier's first bucket and the end of its last one on disk."""
        period, seg = TIER[tier]
        files = files_of(self.path, tier, seg)
        ts = []
        for _, p in {id(f): f for f in (files[:1] + files[-1:])}.values():
            with _vault(p) as v:
                if _has(v, "t"):
                    ts.append(v["t"].read_array())
        t = np.concatenate(ts) if ts else np.zeros(0)
        return (float(t.min()), float(t.max()) + period) if len(t) else None

    def cover(
        self, t0: float, t1: float, tiers: list[str] | None = None, fine: bool = False
    ) -> list[tuple]:
        """``(tier, a, b)`` pieces (``tiers`` finest first; None: all) that read every bucket
        of ``[t0, t1)`` once.

        Coarse first (``fine`` False, for totals): the coarsest tier for the whole buckets it
        holds, finer tiers for the edges (the open hour and minute, partial buckets at the
        ends). Fine first (for a series): the finest tier where it still has rows, a coarser
        one only for its whole buckets before the one holding the finer tier's first row
        (retention deletes whole files, so that is the time it deleted).
        """
        tiers = list(TIER) if tiers is None else tiers
        extents = {t: self.extent(t) for t in tiers}

        def coarse(a: float, b: float, names: list[str]) -> list[tuple]:
            if a >= b or not names:
                return []
            *finer, tier = names
            if not finer:
                return [(tier, a, b)]
            period = TIER[tier][0]
            ext = extents[tier]
            lo = max(math.ceil(a / period) * period, ext[0]) if ext else b
            hi = min(math.floor(b / period) * period, ext[1]) if ext else a
            if lo >= hi:
                return coarse(a, b, finer)
            return [*coarse(a, lo, finer), (tier, lo, hi), *coarse(hi, b, finer)]

        def finest(a: float, b: float, names: list[str]) -> list[tuple]:
            if a >= b or not names:
                return []
            tier, *coarser = names
            ext = extents[tier]
            if ext is None:
                return finest(a, b, coarser)
            if not coarser:
                return [(tier, a, b)]
            period = TIER[coarser[0]][0]
            c = max(a, math.floor(ext[0] / period) * period)
            if c >= b:
                return finest(a, b, coarser)
            return [*finest(a, c, coarser), (tier, c, b)]

        return finest(t0, t1, tiers) if fine else coarse(t0, t1, tiers)

    def covered(
        self,
        t0: float,
        t1: float,
        ents: list[int] | None = None,
        mets: list[int] | None = None,
        tiers: list[str] | None = None,
        fine: bool = False,
    ) -> tuple[Rows, list[tuple]]:
        """Every bucket of ``[t0, t1)`` read once (``cover``), and the pieces read."""
        pieces = self.cover(t0, t1, tiers, fine)
        return concat(*(self.rows(t, a, b, ents, mets) for t, a, b in pieces)), pieces

    def series(
        self,
        entity: int,
        metric: str | int,
        t0: float,
        t1: float,
        points: int = 2000,
        stat: str = "rate",
    ) -> dict:
        """One series of ``stat`` over ``[t0, t1)``: ``t`` (bucket starts), ``v``, and for the
        band ``lo`` / ``hi`` (the least and greatest value inside each bucket).

        ``tier`` is the coarsest one whose bucket fits the point budget (the finest when none
        does), read fine first (``cover``) with the coarser tiers where retention deleted its
        rows; ``step`` is at least the coarsest bucket that returned rows.
        """
        met = L.METRICS[metric] if isinstance(metric, str) else metric
        want = max(1.0, (t1 - t0) / max(1, points))
        names = list(TIER)
        base = max([i for i, n in enumerate(names) if TIER[n][0] <= want], default=0)
        parts = [
            (t, self.rows(t, a, b, [entity], [met]))
            for t, a, b in self.cover(t0, t1, names[base:], fine=True)
        ]
        parts = [(t, r) for t, r in parts if len(r)]
        if not parts:
            return {"tier": None, "step": None, "t": [], "v": [], "lo": [], "hi": []}
        rows = concat(*(r for _, r in parts))
        tier = names[base]
        period = TIER[tier][0]
        step = max(int(np.ceil(want / period)) * period, *(TIER[t][0] for t, _ in parts))
        b = ((rows.t - t0) // step).astype(np.int64)
        out_t, out_v, out_lo, out_hi = [], [], [], []
        for k in np.unique(b):
            m = b == k
            n, s = float(rows.n[m].sum()), float(rows.sum[m].sum())
            lo, hi = float(rows.lo[m].min()), float(rows.hi[m].max())
            h = rows.h[m].sum(axis=0)
            out_t.append(t0 + float(k) * step)
            out_v.append(_stat(stat, n, s, lo, hi, h, step))
            out_lo.append(lo if np.isfinite(lo) else None)
            out_hi.append(hi if np.isfinite(hi) else None)
        return {"tier": tier, "step": step, "t": out_t, "v": out_v, "lo": out_lo, "hi": out_hi}

    def hist(self, entity: int, metric: str | int, t0: float, t1: float) -> dict:
        """The merged histogram of one entity's metric over a range, with its quantiles.

        ``edges`` are the bins' bounds, the last bin's upper one None (it has none)."""
        met = L.METRICS[metric] if isinstance(metric, str) else metric
        rows, _ = self.covered(t0, t1, [entity], [met])
        bounds = [e if math.isfinite(e) else None for e in edges().tolist()]
        if not len(rows):
            return {"n": 0, "edges": bounds, "counts": [0] * BINS}
        h = rows.h.sum(axis=0)
        lo, hi = float(rows.lo.min()), float(rows.hi.max())
        q = {
            f"p{p}": quantile(h.tolist(), p / 100, lo, hi) if h.sum() else None
            for p in (50, 90, 99)
        }
        return {
            "n": int(rows.n.sum()),
            "sum": float(rows.sum.sum()),
            "min": lo if np.isfinite(lo) else None,
            "max": hi if np.isfinite(hi) else None,
            "edges": bounds,
            "counts": h.tolist(),
            **q,
        }

    def top(
        self, kind: str, metric: str | int, t0: float, t1: float, stat: str = "p90", k: int = 10
    ) -> list[dict]:
        """Entities of ``kind`` ranked by ``stat`` of ``metric`` over a range, highest first."""
        met = L.METRICS[metric] if isinstance(metric, str) else metric
        want = L.ENTITY_KINDS[kind]
        rows, _ = self.covered(t0, t1, None, [met])
        rows = rows.take(np.flatnonzero((rows.ent >> np.uint64(56)) == want))
        if not len(rows):
            return []
        out = []
        span = max(1.0, t1 - t0)
        for e in np.unique(rows.ent):
            m = rows.ent == e
            n, s = float(rows.n[m].sum()), float(rows.sum[m].sum())
            lo, hi = float(rows.lo[m].min()), float(rows.hi[m].max())
            v = _stat(stat, n, s, lo, hi, rows.h[m].sum(axis=0), span)
            out.append({"entity": int(e), "id": L.entity_id(int(e)), "n": int(n), "value": v})
        out.sort(key=lambda r: -1e300 if r["value"] is None else -r["value"])
        return out[:k]

    def table(self, kind: str, t0: float, t1: float) -> list[dict]:
        """Every entity of ``kind`` over a range with each of its metrics: count and rate, and
        for a valued metric its mean, minimum, maximum and p50 / p90 / p99."""
        want = L.ENTITY_KINDS[kind]
        names = {v: k for k, v in L.METRICS.items()}
        rows, _ = self.covered(t0, t1)
        rows = rows.take(np.flatnonzero((rows.ent >> np.uint64(56)) == want))
        if not len(rows):
            return []
        span = max(1.0, t1 - t0)
        out: dict[int, dict] = {}
        keys = np.stack([rows.ent, rows.met.astype(np.uint64)], axis=1)
        for e, met in np.unique(keys, axis=0).tolist():
            m = (rows.ent == e) & (rows.met == met)
            n, s = float(rows.n[m].sum()), float(rows.sum[m].sum())
            lo, hi = float(rows.lo[m].min()), float(rows.hi[m].max())
            h = rows.h[m].sum(axis=0)
            stats = {"n": int(n), "rate": n / span}
            if met in L.VALUED:
                stats |= {s_: _stat(s_, n, s, lo, hi, h, span) for s_ in ("mean", "min", "max")}
                stats |= {s_: _stat(s_, n, s, lo, hi, h, span) for s_ in ("p50", "p90", "p99")}
            row = out.setdefault(e, {"entity": e, "id": L.entity_id(e), "metrics": {}})
            row["metrics"][names[met]] = stats
        return list(out.values())

    # ------------------------------------------------------------ records
    def raw(self, t0: float, t1: float, limit: int = 1_000_000) -> np.ndarray:
        """The raw records with wall time in ``[t0, t1)``, oldest first, at most ``limit``."""
        epoch = self.info()["epoch_wall"]
        seg = self.info().get("segment_s") or 600
        out = []
        got = 0
        for start, p in files_of(self.path, "raw", seg):
            if start + seg <= t0 or start >= t1:
                continue
            with _vault(p) as v:
                if not _has(v, "rec"):
                    continue
                recs = v["rec"].read_array().reshape(-1).view(L.REC)
            wall = epoch + recs["t_ns"] / 1e9
            recs = recs[(wall >= t0) & (wall < t1)]
            out.append(recs[: limit - got])
            got += len(out[-1])
            if got >= limit:
                break
        return np.concatenate(out) if out else np.zeros(0, L.REC)

    def traces(
        self, t0: float = 0.0, t1: float = float("inf"), reason: str | None = None, limit: int = 200
    ) -> list[dict]:
        """Kept traces ending in ``[t0, t1)`` (newest first), without their records."""
        p = self.path / L.EVENTS_FILE
        if not p.exists():
            return []
        out = []
        with self._kv(L.EVENTS_FILE) as kv:
            for key, v in kv.items(prefix="trace/"):
                if not (t0 <= v["t_end"] < t1) or (reason and v.get("reason") != reason):
                    continue
                out.append({"key": key.decode(), **{k: v[k] for k in v if k != "records"}})
        out.sort(key=lambda r: -r["t_end"])
        return out[:limit]

    def trace(self, key: str) -> dict | None:
        """One kept trace with its records as dicts (wall time ``t``, kind name)."""
        with self._kv(L.EVENTS_FILE) as kv:
            v = kv.get(key)
        if v is None:
            return None
        epoch = self.info()["epoch_wall"]
        rows = [record_dict(r, epoch) for r in np.frombuffer(v["records"], dtype=L.REC)]
        return {**{k: v[k] for k in v if k != "records"}, "key": key, "records": rows}

    # ------------------------------------------------------------ series of samples
    def _sample_ids(self, names: list[str]) -> tuple[dict[int, str], dict[str, str]]:
        """The series ids of ``names`` (unknown names left out), and each name's unit."""
        known = {v["name"]: (sid, v.get("unit", "")) for sid, v in self.series_names().items()}
        return {known[n][0]: n for n in names if n in known}, {
            n: known[n][1] for n in names if n in known
        }

    def _sample_files(self, sids: dict[int, str], t0: float, t1: float, newest: bool = False):
        """``(start, t, sid, v)`` of each metrics segment overlapping ``[t0, t1)``, only those
        series' finite samples inside the range; oldest file first (``newest``: newest first)."""
        files = files_of(self.path, "metrics", L.METRICS_SEGMENT_S)
        for start, p in reversed(files) if newest else files:
            if start + L.METRICS_SEGMENT_S <= t0 or start >= t1:
                continue
            with _vault(p) as v:
                if not _has(v, "t"):
                    continue
                got = complete({c: v[c].read_array() for c in ("t", "sid", "v")})
            t, sid, val = got["t"], got["sid"], got["v"]
            keep = (t >= t0) & (t < t1) & np.isin(sid, list(sids)) & np.isfinite(val)
            yield start, t[keep], sid[keep], val[keep]

    def samples(self, names: list[str], t0: float, t1: float) -> dict[str, dict]:
        """Every sample of the named series: ``{name: {"t": [...], "v": [...], "unit": str}}``,
        in time order. Unbounded: a long range is read with ``sample_chunks``."""
        sids, units = self._sample_ids(names)
        out = {n: {"t": [], "v": [], "unit": u} for n, u in units.items()}
        got = [(t, sid, val) for _, t, sid, val in self._sample_files(sids, t0, t1)]
        if not got:
            return out
        t, sid, val = (np.concatenate(c) for c in zip(*got))
        order = np.lexsort((t, sid))
        t, sid, val = t[order], sid[order], val[order]
        cut = np.flatnonzero(np.r_[True, np.diff(sid) != 0])
        for i, j in zip(cut, np.r_[cut[1:], len(sid)]):
            o = out[sids[int(sid[i])]]
            o["t"], o["v"] = t[i:j].tolist(), val[i:j].tolist()
        return out

    def sample_chunks(
        self,
        names: list[str],
        t0: float,
        t1: float,
        points: int = 1000,
        chunks: int = 8,
        since: float | None = None,
        step: int | None = None,
    ):
        """The named series over ``[t0, t1)`` in buckets of ``step``, newest first.

        Yields a head ``{"step", "t0", "t1", "since", "units"}``, then chunks ``{"from", "to",
        "series": {name: {"t", "v", "lo", "hi"}}}``, each the buckets of ``[from, to)`` in time
        order, chunks going back in time, at most ``ceil(points / chunks)`` buckets per series.
        ``step`` (``sample_step``) divides a metrics segment, so no bucket spans two files nor
        two chunks. Per bucket: ``t`` / ``v`` the means of its samples' times and values,
        ``lo`` / ``hi`` the least and greatest value.

        A refresh passes the ``step`` it holds and ``since``, the start of its last bucket: if
        the range still gives that step only buckets from ``since`` on are read (the head's
        ``since``); otherwise ``since`` is ``t0``.
        """
        sids, units = self._sample_ids(names)
        want = sample_step(t0, t1, points)
        if since is not None and step == want and since > t0:
            since = max(t0, math.floor(since / step) * step)
        else:
            since = t0
        step = want
        yield {"step": step, "t0": t0, "t1": t1, "since": since, "units": units}
        width = max(1, math.ceil(points / max(1, chunks)))
        for start, t, sid, val in self._sample_files(sids, since, t1, newest=True):
            if not len(t):
                continue
            k = np.floor(t / step).astype(np.int64)
            # Windows of `width` buckets at multiples of it, newest first; each is bucketed
            # only when it is sent (samples arrive nearly in time order: the sort is cheap)
            w = k // width
            by_w = np.argsort(w, kind="stable")
            cuts = np.flatnonzero(np.r_[True, np.diff(w[by_w]) != 0])
            for a, b in zip(cuts[::-1], np.r_[cuts[1:], len(by_w)][::-1]):
                idx = by_w[a:b]
                ww = int(w[idx[0]])
                yield {
                    "from": max(since, start, ww * width * step),
                    "to": min(t1, start + L.METRICS_SEGMENT_S, (ww + 1) * width * step),
                    "series": _buckets(t[idx], sid[idx], val[idx], k[idx], sids),
                }


def _buckets(t, sid, val, k, sids: dict[int, str]) -> dict[str, dict]:
    """Samples bucketed by ``k`` per series: ``{name: {"t", "v", "lo", "hi"}}`` in time order."""
    order = np.lexsort((k, sid))
    t, sid, val, k = t[order], sid[order], val[order], k[order]
    heads = np.flatnonzero(np.r_[True, (np.diff(k) != 0) | (np.diff(sid) != 0)])
    n = np.diff(np.r_[heads, len(t)])
    gsid = sid[heads]
    gt = np.add.reduceat(t, heads) / n
    gv = np.add.reduceat(val, heads) / n
    glo = np.minimum.reduceat(val, heads)
    ghi = np.maximum.reduceat(val, heads)
    cut = np.flatnonzero(np.r_[True, np.diff(gsid) != 0])
    return {
        sids[int(gsid[i])]: {
            "t": gt[i:j].tolist(),
            "v": gv[i:j].tolist(),
            "lo": glo[i:j].tolist(),
            "hi": ghi[i:j].tolist(),
        }
        for i, j in zip(cut, np.r_[cut[1:], len(gsid)])
    }


STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)


def sample_step(t0: float, t1: float, points: int) -> int:
    """The least of ``STEPS`` (seconds, each dividing a day) giving at most ``points`` buckets
    over ``[t0, t1)``; the largest when none does."""
    want = (t1 - t0) / max(1, points)
    return next((s for s in STEPS if s >= want), STEPS[-1])
