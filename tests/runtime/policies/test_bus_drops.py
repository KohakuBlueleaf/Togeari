"""Bus drop counts: a lowered level counts what it skips, and drops outlive the subscription."""

import threading

from togeari.core.observe.bus import Bus


class Held:
    """A subscriber whose thread waits on ``gate`` before its first event returns."""

    name = "held"
    events = "all"
    overflow = "counters_only"
    queue_size = 4

    def __init__(self):
        self.gate = threading.Event()
        self.kinds: list[str] = []

    def on_event(self, ev):
        self.gate.wait(5)
        self.kinds.append(ev.kind)


def test_a_lowered_level_counts_what_it_skips_and_the_count_outlives_the_subscription():
    bus = Bus({"events": "all"})
    held = Held()
    bus.subscribe(held)

    # Fill the queue (the thread holds the first event), then overflow it: the level lowers
    for _ in range(10):
        bus.emit("stmt.fetch", job=1)
    first = bus.dropped()["held"]
    assert first >= 1

    # Detail events at the lowered level are skipped and counted; core events still go in
    for _ in range(5):
        bus.emit("stmt.fetch", job=1)
    assert bus.dropped()["held"] == first + 5

    # The count stays after the subscription ends
    held.gate.set()
    bus.unsubscribe(held)
    assert bus.dropped()["held"] == first + 5
    bus.close()
