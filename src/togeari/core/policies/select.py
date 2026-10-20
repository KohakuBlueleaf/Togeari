"""Unit choice: which units of a lane get work, and which items each takes (spec/04 §4.4)."""

import hashlib
from typing import Any

from togeari.core.contracts.policies import Selector

# Affinity's remembered preferences, per selector, before the memory is cleared.
_PREF_LIMIT = 1 << 16


class LeastLoaded(Selector):
    """Most free capacity first, then the lowest reported waiting count."""

    def order(self, units: list, lane: Any) -> list:
        def score(u):
            try:
                waiting = u.load().waiting
            except Exception:  # noqa: BLE001
                waiting = 1 << 30
            return (-u.capacity(), waiting)

        return sorted(units, key=score)


class Throughput(Selector):
    """Units weighted by measured service rate (items per second, EWMA)."""

    def order(self, units: list, lane: Any) -> list:
        return sorted(units, key=lambda u: (-lane.rate(u), -u.capacity()))


class RoundRobin(Selector):
    """Units in turn: the starting unit moves by one each call."""

    def __init__(self) -> None:
        self.turn = 0

    def order(self, units: list, lane: Any) -> list:
        if not units:
            return []
        self.turn = (self.turn + 1) % len(units)
        return units[self.turn :] + units[: self.turn]


def _rendezvous(key: Any, units: list) -> Any:
    return max(units, key=lambda u: hashlib.sha1(f"{key!r}/{u.name}".encode()).digest())


class Affinity(Selector):
    """The same unit for the same key (e.g. the job), so a unit's per-key cache is reused.

    The preferred unit is the rendezvous hash of the key over the units up, remembered
    per key until the lane's set of units up changes. Any unit takes the item while the
    preferred one has no capacity; unit order is ``LeastLoaded``.
    """

    def __init__(self, key: Any = "job"):
        self.key = key
        self.fallback = LeastLoaded()
        self._pref: dict = {}

    def item_key(self, item: Any) -> Any:
        if self.key == "job":
            return item.ext.get("job_key")
        if callable(self.key):
            return self.key(item)
        return item.inputs.get(self.key)

    def preferred(self, item: Any, lane: Any) -> Any:
        """The item's unit among the lane's units up; None when no unit is up."""
        up = lane.units_up()
        if not up:
            return None

        # Remembered per (lane, key) while the lane's units up are unchanged
        k = (id(lane), self.item_key(item))
        hit = self._pref.get(k)
        if hit is not None and hit[0] == lane.up_version:
            return hit[1]
        if len(self._pref) >= _PREF_LIMIT:
            self._pref.clear()
        unit = _rendezvous(k[1], up)
        self._pref[k] = (lane.up_version, unit)
        return unit

    def order(self, units: list, lane: Any) -> list:
        return self.fallback.order(units, lane)

    def admits(self, item: Any, unit: Any, lane: Any) -> bool:
        pref = self.preferred(item, lane)
        return pref is None or pref is unit or pref.capacity() <= 0


class ByHint(Selector):
    """The unit named by ``@hint unit="..."``; anything else, or a name no unit up has, goes
    anywhere."""

    def __init__(self, fallback: Selector | None = None):
        self.fallback = fallback or LeastLoaded()

    def order(self, units: list, lane: Any) -> list:
        return self.fallback.order(units, lane)

    def admits(self, item: Any, unit: Any, lane: Any) -> bool:
        want = item.ext.get("hint", {}).get("unit")
        if want is None or want == unit.name:
            return True
        return all(u.name != want for u in lane.units_up())


__all__ = ["Affinity", "ByHint", "LeastLoaded", "RoundRobin", "Selector", "Throughput"]
