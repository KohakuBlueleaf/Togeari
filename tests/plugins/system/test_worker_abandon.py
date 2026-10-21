"""The plugin worker's abandon: a closer waiting on the writer's lock gets it at once."""

import threading
import time

import pytest

from togeari.plugins.common.worker import Worker


@pytest.mark.serial
def test_abandon_lets_a_closer_take_the_lock_a_busy_writer_holds_per_task():
    io = threading.Lock()
    handled: list[int] = []

    def handle(task: int) -> None:
        with io:
            time.sleep(0.02)
            handled.append(task)

    # 300 tasks of 20 ms each would hold the lock, task after task, for 6 s
    worker = Worker("test-abandon", handle)
    for i in range(300):
        worker.put(i)
    time.sleep(0.1)

    t0 = time.monotonic()
    worker.abandon()
    with io:
        waited = time.monotonic() - t0
        count = len(handled)
    worker.thread.join(timeout=2)

    assert waited < 1.0
    assert not worker.thread.is_alive()
    assert len(handled) <= count + 1 < 300


def test_close_still_drains_every_task_when_not_abandoned():
    handled: list[int] = []
    worker = Worker("test-drain", handled.append)
    for i in range(1000):
        worker.put(i)
    worker.close(timeout=5)
    assert handled == list(range(1000))
    assert not worker.thread.is_alive()
