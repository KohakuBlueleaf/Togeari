"""Job traces: every record of a job, kept for the jobs worth looking at.

Records are held per job for ``window_s`` (at most ``max_records`` per job, the newest kept).
At a job's end its trace is written when the job failed or faulted, when the job is in the
head sample (``rate``, by job id), or, at the end of each minute, when it was the slowest job
of its entry in that minute.
"""

import time

import numpy as np

from togeari.core.contracts import telemetry as T

# Head sampling by job id: a fixed odd multiplier spreads consecutive ids
_MIX = 0x9E3779B97F4A7C15


class Traces:
    def __init__(self, window_s: float, max_records: int, rate: float):
        self.window_s = window_s
        self.max_records = max_records
        self.rate = rate
        self.jobs: dict[int, list[np.ndarray]] = {}
        self.sizes: dict[int, int] = {}
        self.seen: dict[int, float] = {}
        self.faulted: set[int] = set()
        self.slowest: dict[int, tuple] = {}
        self.minute: int | None = None
        self.cut = 0
        self.evicted = 0

    def sampled(self, job: int) -> bool:
        if self.rate <= 0:
            return False
        return ((job * _MIX) & 0xFFFFFFFFFFFFFFFF) < self.rate * 2**64

    def add(self, recs: np.ndarray, epoch_wall: float) -> list[tuple[str, dict]]:
        """Take a batch of records; the traces to write now, as ``(key, value)``."""
        out: list[tuple[str, dict]] = []
        now = time.monotonic()
        if len(recs):
            order = np.argsort(recs["job"], kind="stable")
            r = recs[order]
            jobs, starts = np.unique(r["job"], return_index=True)
            for job, a, z in zip(jobs.tolist(), starts, [*starts[1:], len(r)]):
                self._hold(job, r[a:z], now)
            for job in r["job"][r["kind"] == T.FAULT].tolist():
                self.faulted.add(job)
            for rec in r[r["kind"] == T.END]:
                out.extend(self._ended(rec, epoch_wall))

        # A minute closed: its slowest job per entry
        minute = int((time.time()) // 60)
        if self.minute is not None and minute != self.minute:
            out.extend((k, v) for k, v in self.slowest.values())
            self.slowest.clear()
        self.minute = minute

        # Jobs not seen for the window (their end was never met) are let go
        stale = [j for j, t in self.seen.items() if now - t > self.window_s]
        for j in stale:
            self._drop(j)
        self.evicted += len(stale)
        return out

    def flush(self) -> list[tuple[str, dict]]:
        """The slowest jobs of the open minute (at stop)."""
        out = [(k, v) for k, v in self.slowest.values()]
        self.slowest.clear()
        return out

    def _hold(self, job: int, part: np.ndarray, now: float) -> None:
        parts = self.jobs.setdefault(job, [])
        parts.append(part.copy())
        self.sizes[job] = self.sizes.get(job, 0) + len(part)
        self.seen[job] = now
        while self.sizes[job] > self.max_records and len(parts) > 1:
            self.sizes[job] -= len(parts[0])
            self.cut += len(parts.pop(0))
        if self.sizes[job] > self.max_records:
            keep = parts[0][-self.max_records :]
            self.cut += len(parts[0]) - len(keep)
            parts[0] = keep
            self.sizes[job] = len(keep)

    def _drop(self, job: int) -> np.ndarray | None:
        parts = self.jobs.pop(job, None)
        self.sizes.pop(job, None)
        self.seen.pop(job, None)
        self.faulted.discard(job)
        return np.concatenate(parts) if parts else None

    def _ended(self, end: np.void, epoch_wall: float) -> list[tuple[str, dict]]:
        job = int(end["job"])
        faulted = job in self.faulted
        recs = self._drop(job)
        if recs is None:
            return []
        t_end = epoch_wall + int(end["t_ns"]) / 1e9
        wall_us = int(end["service_us"])
        status = int(end["status"])
        entry = int(end["addr"])
        value = {
            "job": job,
            "entry": entry,
            "status": status,
            "faulted": faulted,
            "wall_us": wall_us,
            "t_end": t_end,
            "records": recs.tobytes(),
        }
        key = f"trace/{int(t_end * 1000):016d}/{job}"
        if status != 0 or faulted:
            return [(key, {**value, "reason": "failed"})]
        if self.sampled(job):
            return [(key, {**value, "reason": "sampled"})]
        best = self.slowest.get(entry)
        if best is None or wall_us > best[1]["wall_us"]:
            self.slowest[entry] = (key, {**value, "reason": "slowest"})
        return []
