"""A plugin with close_s unloads by one deadline: its queued events and its drain share it."""

import threading
import time

import pytest
from plugin_programs import PROGRAM, jobs

from conftest import build, run_jobs
from togeari import BasePlugin


class SlowListener(BasePlugin):
    """Takes 50 ms per event, so a backlog of events takes seconds to deliver."""

    name = "slow_listener"
    events = "all"
    close_s = 0.5

    def __init__(self):
        self.seen = 0
        self.unloaded_at: float | None = None
        self.late = threading.Event()

    def on_event(self, ev):
        if self.unloaded_at is not None:
            self.late.set()
        time.sleep(0.05)
        self.seen += 1

    def on_unload(self):
        self.unloaded_at = time.monotonic()


@pytest.mark.serial
def test_stop_returns_within_close_s_with_a_backlog_of_slow_events():
    listener = SlowListener()
    m = build(PROGRAM, "main", plugins=[listener], events="all")
    run_jobs(m, jobs(20))

    # Hundreds of queued events, 50 ms each: delivering them all would take many seconds
    t0 = time.monotonic()
    m.stop()
    took = time.monotonic() - t0
    assert took < listener.close_s + 2.0
    assert listener.seen < 100

    # Events dropped at the deadline are not delivered after the unload
    time.sleep(0.2)
    assert not listener.late.is_set()
