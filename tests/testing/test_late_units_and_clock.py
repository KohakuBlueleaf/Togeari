"""A unit that starts after its start timed out is stopped; a raising timer callback is survived."""

import threading
import time

from togeari.core.construct.units import start_all
from togeari.testing.sim import _Clock


class SlowStart:
    name = "slow"

    def __init__(self, delay: float):
        self.delay = delay
        self.stopped = threading.Event()

    def start(self) -> None:
        time.sleep(self.delay)

    def ready(self) -> bool:
        return True

    def stop(self) -> None:
        self.stopped.set()


def test_a_unit_that_finishes_starting_after_its_timeout_is_stopped():
    unit = SlowStart(0.5)
    failures = start_all([unit], timeout=0.1)
    assert [problem for _, problem in failures] == ["start() timed out after 0.1s"]
    assert unit.stopped.wait(3.0)


def test_a_unit_that_starts_in_time_is_not_stopped():
    unit = SlowStart(0.0)
    assert start_all([unit], timeout=2.0) == []
    time.sleep(0.1)
    assert not unit.stopped.is_set()


def test_the_clock_keeps_delivering_after_a_raising_callback():
    clock = _Clock("test")
    clock.start()
    delivered = threading.Event()

    def boom():
        raise RuntimeError("a failing delivery")

    try:
        now = time.monotonic()
        clock.at(now, boom)
        clock.at(now + 0.05, delivered.set)
        assert delivered.wait(2.0)
    finally:
        clock.stop()
