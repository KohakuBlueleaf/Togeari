"""Watches the worker processes of a ``PythonCallEngine`` in process mode.

One thread waits on every worker's process sentinel and a wake pipe. A process
that exits while its slot is idle is reported through ``engine.idle_death``; a
process that exits under an item is its slot's own business, and the monitor
ignores it until the slot has a new process (``poke``).
"""

import contextlib
import multiprocessing
import threading
from multiprocessing import connection
from typing import Any


class Monitor:
    def __init__(self, engine: Any):
        self.engine = engine
        self.wake_r, self.wake_w = multiprocessing.Pipe(duplex=False)
        self.wake_lock = threading.Lock()
        self.closing = False
        self.thread = threading.Thread(target=self.loop, name=f"{engine.name}-monitor", daemon=True)
        self.thread.start()

    def poke(self) -> None:
        """Rebuild the watched set (a slot has a new process, or slots changed)."""
        with self.wake_lock:
            if self.closing:
                return
            with contextlib.suppress(OSError):
                self.wake_w.send(None)

    def close(self) -> None:
        with self.wake_lock:
            self.closing = True
            with contextlib.suppress(OSError):
                self.wake_w.send(None)
        self.thread.join(5.0)
        self.wake_r.close()
        self.wake_w.close()

    def loop(self) -> None:
        engine = self.engine
        ignored: dict[int, Any] = {}
        while not self.closing:
            with engine.lock:
                procs = [(s, s.proc) for s in engine.slots if s.proc is not None]
            ignored = {id(p): p for _, p in procs if ignored.get(id(p)) is p}
            watched = {p.sentinel: (s, p) for s, p in procs if id(p) not in ignored}

            for ready in connection.wait([self.wake_r, *watched]):
                if ready is self.wake_r:
                    while self.wake_r.poll():
                        self.wake_r.recv()
                    continue
                slot, proc = watched[ready]
                if not engine.idle_death(slot, proc):
                    ignored[id(proc)] = proc
