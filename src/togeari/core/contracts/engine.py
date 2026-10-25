"""The engine contracts: what every engine provides and how it behaves (spec/04).

Behaviour every engine keeps:

- B1 ``submit`` never blocks; work runs on the engine's own threads / processes.
- B2 exactly one result per item, delivered through ``done``, from any thread.
- B3 the machine calls ``submit``, ``cancel``, ``capacity`` and ``load`` from the
  dispatcher thread only; ``done`` is the only call that crosses threads.
- B4 the lane never exceeds ``capacity()``; extra work is still answered.
- B5 after ``cancel`` the result is the normal one or ``Cancelled()``.
- B6 a batch engine's result for an item does not depend on its batch mates.
- B7 ``ItemError``: this item failed. ``UnitError``: the whole unit is blocked.
- B8 engines see work items only; equal payloads are separate requests.
"""

from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from togeari.core.contracts.workitem import WorkItem

Kind = Literal["stream", "batch", "call"]
Work = WorkItem | list[WorkItem]
Done = Callable[[Any, list], None]


@dataclass
class Load:
    """What an engine reports about itself; ``extra`` is free-form."""

    running: int = 0
    waiting: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Engine(Protocol):
    """What every engine provides."""

    kind: Kind
    name: str
    # Payload family its adapters speak; None takes the machine's default protocol.
    protocol: str | None
    # True when it runs in the machine's process and sees the caller's objects.
    shares_memory: bool

    def start(self) -> None:
        """Connect, load, spawn; may be slow."""

    def ready(self) -> bool:
        """Health; polled with backoff while the unit is down."""

    def stop(self) -> None: ...

    def spec(self) -> dict:
        """Identity for store keys and the run report."""

    def capacity(self) -> int:
        """How many more submits it accepts now."""

    def load(self) -> Load:
        """Running, waiting, plus any numbers the engine wants reported."""

    def submit(self, work: Work, done: Done) -> Any:
        """Never blocks; returns a ticket."""

    def cancel(self, ticket: Any) -> None:
        """Best effort."""

    # Optional: ``wake_at() -> float | None``, the time.monotonic() time when
    # capacity() may rise without a completion (a rate limit refilling).
    # Optional: ``add_wake(fn)``; the engine calls every added ``fn()`` (from any
    # thread) when capacity() rose without a completion (a worker replaced, a
    # cancelled item's slot freed).
    # Optional: ``metrics() -> dict[str, float | tuple[float, str]]``, numbers the
    # telemetry plugin records as series (a value, or a value and its unit). It is
    # called from the telemetry thread, not the dispatcher, so it must be
    # thread-safe and cheap (B3 does not apply to it).


class StreamEngine(Engine, Protocol):
    """Queues and batches internally; the lane only paces it."""


class BatchEngine(Engine, Protocol):
    """Runs a list of items per submit; the lane forms the batches."""

    max_batch: int

    def key(self, item: WorkItem) -> Hashable:
        """Items with equal keys may share a batch."""


class CallEngine(Engine, Protocol):
    """Runs one item per submit, up to capacity() at once."""


class EngineBase:
    """Optional base with the contract's defaults; engines may ignore it."""

    kind: Kind = "call"
    protocol: str | None = None
    shares_memory: bool = True
    max_batch: int = 1

    def __init__(self, name: str | None = None):
        self.name = name or type(self).__name__.lower()

    def start(self) -> None:
        return None

    def ready(self) -> bool:
        return True

    def stop(self) -> None:
        return None

    def spec(self) -> dict:
        return {"engine": type(self).__qualname__, "name": self.name}

    def load(self) -> Load:
        return Load()

    def cancel(self, ticket: Any) -> None:
        return None

    def wake_at(self) -> float | None:
        return None

    def add_wake(self, fn: Callable[[], None]) -> None:
        self.__dict__.setdefault("_wakes", []).append(fn)

    def wake(self) -> None:
        """Call every added wake function: capacity rose without a completion."""
        for fn in list(self.__dict__.get("_wakes", ())):
            fn()

    def key(self, item: WorkItem) -> Hashable:
        return None
