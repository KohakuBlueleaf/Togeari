"""A dispatcher dies once, even when two threads declare it dead at the same moment."""

import contextlib
import logging
import threading

from rt_helpers import machine

logging.disable(logging.CRITICAL)


def test_two_threads_declaring_death_at_once_run_it_once():
    m = machine()
    d = m.dispatcher
    emitted: list = []
    m.bus.listen(lambda kind, fields: emitted.append(kind), kinds=["machine.error"])

    # The first check of `dead` by each thread waits until both threads have made it
    barrier = threading.Barrier(2, timeout=2)
    gate = {"hold": True}

    class Gated(type(d)):
        @property
        def dead(self):
            value = self.__dict__.get("_dead")
            if gate["hold"] and value is None:
                gate["hold"] = threading.current_thread().name != "second"
                with contextlib.suppress(threading.BrokenBarrierError):
                    barrier.wait()
            return value

        @dead.setter
        def dead(self, value):
            self.__dict__["_dead"] = value

    d.__dict__["_dead"] = None
    d.__class__ = Gated
    try:
        threads = [
            threading.Thread(target=d.died, args=(RuntimeError("a"),), name="first"),
            threading.Thread(target=d.died, args=(RuntimeError("b"),), name="second"),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert emitted == ["machine.error"]
    finally:
        m.stop()
