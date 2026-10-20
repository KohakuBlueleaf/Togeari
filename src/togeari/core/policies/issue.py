"""Issue order of ready statements across jobs (spec/05 §5.2).

Policies read ``ReadyView`` objects (``togeari.core.contracts.views``).
"""

from togeari.core.contracts.views import ReadyView


class Fifo:
    """Ready order as it came."""

    def order(self, ready: list[ReadyView]) -> list[ReadyView]:
        return ready

    def priority(self, st: ReadyView) -> int:
        return 0


class OldestJobFirst:
    """Finish jobs (and free their memory) before starting work for newer ones."""

    def order(self, ready: list[ReadyView]) -> list[ReadyView]:
        return sorted(ready, key=lambda r: (r.job, r.key))

    def priority(self, st: ReadyView) -> int:
        return -st.job


class DeepestFirst:
    """Prefer statements later in program order (deeper into their job)."""

    def order(self, ready: list[ReadyView]) -> list[ReadyView]:
        return sorted(ready, key=lambda r: r.key, reverse=True)

    def priority(self, st: ReadyView) -> int:
        return len(st.key)


class LaneAware:
    """Feed the lane closest to starving first (the shortest queue per unit up).

    Each lane's fill is read once per ``order`` call.
    """

    def order(self, ready: list[ReadyView]) -> list[ReadyView]:
        fill: dict = {}

        def starve(r: ReadyView) -> tuple:
            lane = r.lane
            if lane is None:
                return (-1, r.key)
            f = fill.get(lane)
            if f is None:
                f = fill[lane] = r.lane_fill
            return (f, r.key)

        return sorted(ready, key=starve)

    def priority(self, st: ReadyView) -> int:
        return 0


class HintPriority:
    """``@hint priority=int`` (higher first), then program order."""

    def order(self, ready: list[ReadyView]) -> list[ReadyView]:
        return sorted(ready, key=lambda r: (-int(r.hint.get("priority", 0)), r.key))

    def priority(self, st: ReadyView) -> int:
        return int(st.hint.get("priority", 0))
