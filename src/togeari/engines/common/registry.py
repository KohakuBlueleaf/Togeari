"""The registry of engine worker processes (``togeari doctor`` reads it).

Each worker process of a builtin engine registers itself while it runs: one
file ``<pid>.json`` in ``REGISTRY`` (the system temporary directory) holding
its pid, its parent's pid, its entry point (the child module) and its start
time. The file is removed when the worker returns; a killed worker leaves it,
and ``togeari doctor`` removes the files of processes that no longer exist.
"""

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

REGISTRY = Path(tempfile.gettempdir()) / "togeari-workers"


def _file(pid: int) -> Path:
    return REGISTRY / f"{pid}.json"


@contextlib.contextmanager
def registered(entry: str) -> Iterator[None]:
    """Register this process as a worker running ``entry`` for the duration of the block."""
    pid = os.getpid()
    info = {"pid": pid, "parent": os.getppid(), "entry": entry, "started": time.time()}
    with contextlib.suppress(OSError):
        REGISTRY.mkdir(parents=True, exist_ok=True)
        _file(pid).write_text(json.dumps(info), encoding="utf-8")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            _file(pid).unlink()


def workers() -> list[dict]:
    """Every registered worker's info (live or left by a killed process)."""
    found = []
    for path in sorted(REGISTRY.glob("*.json")) if REGISTRY.is_dir() else []:
        with contextlib.suppress(OSError, ValueError):
            found.append(json.loads(path.read_text(encoding="utf-8")))
    return found


def forget(pid: int) -> None:
    """Remove a worker's file (its process no longer exists)."""
    with contextlib.suppress(OSError):
        _file(pid).unlink()
