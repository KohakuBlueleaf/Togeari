"""A peer thread in this process, reached through two ``SimpleQueue``s; objects pass by
reference.
"""

import queue
import threading
import time
from collections import deque


class ThreadLink:
    name = "thread"

    def __init__(self) -> None:
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.outbox: queue.SimpleQueue = queue.SimpleQueue()
        self.thread = threading.Thread(target=self._peer, name="s1-peer", daemon=True)

    def open(self) -> None:
        self.thread.start()

    def _peer(self) -> None:
        while True:
            msg = self.inbox.get()
            match msg:
                case ("one", _, _):
                    self.outbox.put(("t", time.perf_counter()))
                case ("echo", obj):
                    self.outbox.put(("echo", obj))
                case ("stop",):
                    return

    def oneway(self, obj: object) -> float:
        t0 = time.perf_counter()
        self.inbox.put(("one", obj, t0))
        _, t = self.outbox.get()
        return t - t0

    def echo(self, obj: object) -> float:
        t0 = time.perf_counter()
        self.inbox.put(("echo", obj))
        self.outbox.get()
        return time.perf_counter() - t0

    def stream(self, objs: list, window: int) -> float:
        t0 = time.perf_counter()
        pending = deque()
        for obj in objs:
            if len(pending) >= window:
                self.outbox.get()
                pending.popleft()
            self.inbox.put(("one", obj, time.perf_counter()))
            pending.append(1)
        for _ in pending:
            self.outbox.get()
        return time.perf_counter() - t0

    def peer_cpu(self) -> float | None:
        return None

    def close(self) -> None:
        self.inbox.put(("stop",))
        self.thread.join(5.0)
