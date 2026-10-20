"""PythonCallEngine revive: workers lost while a revive runs are replaced by that revive."""

import time

from togeari.engines.python import PythonCallEngine


def until(cond, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not (value := cond()) and time.monotonic() < deadline:
        time.sleep(0.01)
    return value


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
