"""What callers hold while the dispatcher works: job handles, replies, a run's state."""

import threading
from typing import Any

from togeari.core.contracts.io import JobRecord


class DispatcherDied(RuntimeError):
    """The dispatcher thread stopped on an error; every waiting caller gets this."""


class JobHandle:
    """A programmatic job; ``result(timeout)`` waits for its record. The first record set is
    the one kept."""

    def __init__(self, key: str):
        self.key = key
        self._ev = threading.Event()
        self._lock = threading.Lock()
        self.record: JobRecord | None = None

    def _set(self, record: JobRecord) -> None:
        with self._lock:
            if self._ev.is_set():
                return
            self.record = record
            self._ev.set()

    def done(self) -> bool:
        return self._ev.is_set()

    def result(self, timeout: float | None = None) -> JobRecord:
        if not self._ev.wait(timeout):
            raise TimeoutError(f"job {self.key} not finished")
        return self.record


class Reply:
    """The answer of a query or command posted to the inbox. The first answer set is the one
    kept."""

    def __init__(self) -> None:
        self._ev = threading.Event()
        self._lock = threading.Lock()
        self.value: Any = None
        self.error: BaseException | None = None

    def _set(self, value: Any = None, error: BaseException | None = None) -> None:
        with self._lock:
            if self._ev.is_set():
                return
            self.value, self.error = value, error
            self._ev.set()

    def done(self) -> bool:
        return self._ev.is_set()

    def result(self, timeout: float | None = 30.0) -> Any:
        if not self._ev.wait(timeout):
            raise TimeoutError("no answer from the dispatcher")
        if self.error is not None:
            raise self.error
        return self.value


class RunState:
    """One ``Machine.run``: set ``done`` when its source is drained and its jobs have ended."""

    __slots__ = ("done", "source_finished", "source_error", "sink_error", "stopped")

    def __init__(self) -> None:
        self.done = threading.Event()
        # Set by the source reader after its last put
        self.source_finished = False
        self.source_error: BaseException | None = None
        self.sink_error: BaseException | None = None
        self.stopped = False


# The last item of the sink queue
SINK_END = object()
