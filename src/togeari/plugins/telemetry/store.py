"""Segmented column files: one ColumnVault per time segment, written by one thread.

A tier's rows go to ``<dir>/<name>/<start>.cv`` by their time (``<dir>/<name>.cv`` when the
tier is one file). The writer keeps the newest segment open; an older one is checkpointed and
closed when a newer one opens. Retention deletes whole files, oldest first, so no row is ever
deleted from a file. Readers open each file read-only for one call.
"""

import contextlib
import logging
from pathlib import Path

import numpy as np
from kohakuvault import ColumnVault

log = logging.getLogger("togeari.telemetry")


def files_of(root: Path, name: str, seg_s: int | None) -> list[tuple[int, Path]]:
    """``(start, path)`` of a tier's files, oldest first (start 0 for a one-file tier)."""
    if seg_s is None:
        p = root / f"{name}.cv"
        return [(0, p)] if p.exists() else []
    d = root / name
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.cv"):
        with contextlib.suppress(ValueError):
            out.append((int(p.stem), p))
    return sorted(out)


def remove(path: Path) -> bool:
    """Delete a column file and its SQLite sidecars; False when one is still open elsewhere."""
    ok = True
    for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            ok = False
    return ok


class Segments:
    """One tier's files: ``columns`` maps a column name to its ColumnVault dtype."""

    def __init__(self, root: Path, name: str, columns: dict[str, str], seg_s: int | None):
        self.root = root
        self.name = name
        self.columns = columns
        self.seg_s = seg_s
        self.start: int | None = None
        self.vault: ColumnVault | None = None
        self.cols: dict = {}
        self.rows = 0

    def path(self, start: int) -> Path:
        if self.seg_s is None:
            return self.root / f"{self.name}.cv"
        return self.root / self.name / f"{start:012d}.cv"

    def _open(self, start: int) -> None:
        if self.vault is not None and start == self.start:
            return
        self.close()
        p = self.path(start)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.vault = ColumnVault(str(p))
        self.cols = {n: self.vault.ensure(n, d) for n, d in self.columns.items()}
        self.start = start

    def append(self, t: np.ndarray, data: dict) -> None:
        """Append rows (``t``: unix seconds per row, nondecreasing within a call) to their
        segments; ``data`` holds an array (or a list, for a bytes column) per column."""
        if not len(t):
            return
        if self.seg_s is None:
            self._write(0, data, slice(None))
            return
        seg = (t // self.seg_s) * self.seg_s
        cuts = np.flatnonzero(np.diff(seg)) + 1
        for a, z in zip(np.r_[0, cuts], np.r_[cuts, len(t)]):
            self._write(int(seg[a]), data, slice(int(a), int(z)))

    def _write(self, start: int, data: dict, sel: slice) -> None:
        self._open(start)
        for n, col in self.cols.items():
            v = data[n][sel]
            if isinstance(v, list):
                col.extend(v)
            else:
                col.append_array(np.ascontiguousarray(v))
        self.rows += len(next(iter(data.values()))[sel])

    def close(self) -> None:
        if self.vault is None:
            return
        try:
            self.vault.checkpoint()
            self.vault.close()
        except Exception:
            log.warning("telemetry: closing %s failed", self.path(self.start or 0), exc_info=True)
        self.vault = None
        self.cols = {}
        self.start = None

    def retain(self, now: float, keep_s: float | None, keep_bytes: int | None) -> int:
        """Delete files older than ``keep_s`` or past ``keep_bytes`` in total (oldest first,
        never the open one); the number deleted."""
        if self.seg_s is None:
            return 0
        files = [(s, p) for s, p in files_of(self.root, self.name, self.seg_s) if s != self.start]
        sizes = {p: _size(p) for _, p in files}
        total = sum(sizes.values()) + (_size(self.path(self.start)) if self.start else 0)
        deleted = 0
        for s, p in files:
            old = keep_s is not None and s + self.seg_s <= now - keep_s
            big = keep_bytes is not None and total > keep_bytes
            if not (old or big):
                break
            if remove(p):
                deleted += 1
                total -= sizes[p]
        return deleted


def _size(p: Path) -> int:
    n = 0
    for q in (p, Path(f"{p}-wal")):
        with contextlib.suppress(OSError):
            n += q.stat().st_size
    return n
