"""Sources, sinks, stores and job records (spec/05)."""

import copy
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class SourceItem:
    """One job to run; ``entry`` None means the machine's default entry.

    ``auto_key`` marks a key the source or the machine made (a source's item index, or the
    process-wide counter of keyless submits) rather than one given. ``checkpoint`` explicitly
    resumes a pinned artifact; absence means a normal submission, never a pinned-artifact search.
    """

    key: str
    inputs: dict
    entry: str | None = None
    auto_key: bool = False
    checkpoint: str | None = None


@dataclass
class JobRecord:
    """A finished job: outputs or failure, stats, key; ``record`` holds its conformance record."""

    key: str
    entry: str
    ok: bool
    outputs: dict = field(default_factory=dict)
    failure: dict | None = None
    missing: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    record: list | None = None


@runtime_checkable
class Source(Protocol):
    def __iter__(self) -> Iterator[SourceItem]:
        """Yields SourceItem(key, inputs, entry=None)."""

    def shard(self, rank: int, world: int) -> "Source": ...


@runtime_checkable
class Sink(Protocol):
    def write(self, record: JobRecord) -> None:
        """Outputs or failure, stats, key."""

    def close(self) -> None: ...


@runtime_checkable
class Store(Protocol):
    """Results by key. ``put`` keeps its own copy; ``get`` returns a fresh copy, or ``MISS``.

    An optional ``delete(key)`` removes a key (finished jobs' snapshots).
    """

    def get(self, key: bytes) -> Any: ...

    def put(self, key: bytes, outputs: Any) -> None: ...


MISS = object()


def copy_value(value: Any) -> Any:
    """A copy no change to ``value`` reaches: the value's ``__togeari_copy__``, or a deep copy."""
    fn = getattr(value, "__togeari_copy__", None)
    return fn() if fn is not None else copy.deepcopy(value)
