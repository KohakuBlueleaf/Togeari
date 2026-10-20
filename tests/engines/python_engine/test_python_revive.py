"""PythonCallEngine revive: workers lost while a revive runs are replaced by that revive."""

import time

from togeari.engines.python import PythonCallEngine


def until(cond, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not (value := cond()) and time.monotonic() < deadline:
        time.sleep(0.01)
    return value


def test_a_revive_that_opens_nothing_retries_while_the_engine_is_up():
    engine = PythonCallEngine(workers=2, mode="thread", revive_backoff_s=0.05)
    engine.start()
    real_open = engine._open
    failures = [2]

    # The first two attempts open no slot
    def flaky_open(n, epoch):
        if failures[0] > 0:
            failures[0] -= 1
            return []
        return real_open(n, epoch)

    engine._open = flaky_open
    try:
        lost = engine.slots.pop()
        lost.exit = True
        with engine.lock:
            engine.reviving = True
        engine._revive()
        assert len(engine.slots) == 2 and not engine.reviving
        assert engine.capacity() == 2
    finally:
        engine.stop()


def test_a_worker_lost_during_a_revive_is_replaced_too():
    engine = PythonCallEngine(workers=3, mode="process")
    engine.start()
    try:
        # One worker dies; while its replacement boots, another one dies
        first = engine.slots[0]
        first.proc.kill()
        assert until(lambda: engine.reviving, timeout=10.0)
        second = next(s for s in list(engine.slots) if s is not first)
        second.proc.kill()

        # Every slot comes back
        assert until(lambda: engine.capacity() == 3 and not engine.reviving, timeout=60.0)
        time.sleep(0.5)
        assert engine.capacity() == 3
        assert engine.load().extra["live"] == 3
    finally:
        engine.stop()
