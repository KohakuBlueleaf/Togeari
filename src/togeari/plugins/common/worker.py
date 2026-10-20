"""A background thread that drains a bounded queue of tasks with one handler."""

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("togeari.plugins")

_STOP = object()


class Worker:
    """Runs ``handle(task)`` for every task put, in order, on its own thread.

    The queue holds at most ``maxsize`` tasks; ``put`` never blocks, and a task
    that finds the queue full is dropped and counted in ``dropped``. ``idle``
    runs after each burst of tasks and after every quiet half second. After
    ``abandon`` the thread finishes the task it is running and ends, skipping the rest.
    """

    def __init__(
        self,
        name: str,
        handle: Callable[[Any], None],
        idle: Callable[[], None] | None = None,
        maxsize: int = 100_000,
    ):
        self.handle = handle
        self.idle = idle
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self.abandoned = False
        self._count = threading.Lock()
        self.thread = threading.Thread(target=self.loop, name=name, daemon=True)
        self.thread.start()

    def put(self, task: Any) -> bool:
        """Queue ``task``; False when it was dropped."""
        try:
            self.q.put_nowait(task)
        except queue.Full:
            with self._count:
                self.dropped += 1
            return False
        return True

    def loop(self) -> None:
        while not self.abandoned:
            try:
                task = self.q.get(timeout=0.5)
            except queue.Empty:
                task = None

            if task is None:
                if self.idle is not None:
                    self._safe(self.idle)
                continue
            if task is _STOP or self.abandoned:
                break
            self._safe(self.handle, task)
            if self.idle is not None and self.q.empty():
                self._safe(self.idle)

    @staticmethod
    def _safe(fn: Callable, *args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            log.warning("plugin worker task failed", exc_info=True)

    def close(self, timeout: float = 10.0) -> None:
        """Finish the queued tasks, then stop, all within ``timeout``."""
        deadline = time.monotonic() + timeout
        try:
            self.q.put(_STOP, timeout=max(0.0, timeout))
        except queue.Full:
            log.warning("plugin worker %s did not drain in %ss", self.thread.name, timeout)
            return
        self.thread.join(timeout=left(deadline))

    def abandon(self) -> None:
        """Skip every queued task; the running one finishes, then the thread ends. Takes no
        lock."""
        self.abandoned = True


def left(deadline: float | None, default: float = 10.0) -> float:
    """Seconds until ``deadline`` (monotonic), at least 0; ``default`` when there is none."""
    if deadline is None:
        return default
    return max(0.0, deadline - time.monotonic())
