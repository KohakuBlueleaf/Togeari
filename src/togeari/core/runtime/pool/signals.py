"""Admission signals (spec/05 §5.1): read on demand, so a rule pays only for what it reads."""

from collections.abc import Mapping
from typing import Any

KEYS = ("jobs", "bytes", "window", "lanes", "units")


def unit_load(u: Any) -> dict:
    try:
        ld = u.load()
        return {"running": ld.running, "waiting": ld.waiting, **ld.extra}
    except Exception:  # noqa: BLE001
        return {}


class Signals(Mapping):
    """``jobs`` and ``bytes`` are live; ``window``, ``lanes``, ``units`` are computed on first read.

    Only the dispatcher thread reads one; ``to_dict()`` is a plain copy for other threads.
    """

    def __init__(self, d: Any):
        self.d = d
        self._cache: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        match key:
            case "jobs":
                return self.d.job_count()
            case "bytes":
                self.d.release_freed()
                return self.d.pool_bytes
            case "window" | "lanes" | "units" if key in self._cache:
                return self._cache[key]
            case "window":
                v = self.d.window_total()
            case "lanes":
                v = {lane.name: lane.signals() for lane in self.d.lanes}
            case "units":
                v = {u.name: unit_load(u) for lane in self.d.lanes for u in lane.units}
            case _:
                raise KeyError(key)
        self._cache[key] = v
        return v

    def __iter__(self):
        return iter(KEYS)

    def __len__(self) -> int:
        return len(KEYS)

    def to_dict(self) -> dict:
        return {k: self[k] for k in KEYS}
