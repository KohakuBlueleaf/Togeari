"""Semantic committed-event transport contracts, separate from the lossy diagnostic bus.

Journal append acknowledges commitment; sink success acknowledges delivery. The latter is
at least once, never an exactly-once promise. Payloads are KIR's encoded portable DTOs.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from togeari.core.contracts.identity import validate_invocation_id, validate_job_execution_id


class EventConflict(ValueError):
    """An event ID or logical sequence was reused with different committed content."""


class EventTooLarge(ValueError):
    """One prepared event cannot fit the configured bounded outbox."""


def _json_value(value: Any, active: set[int] | None = None) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("event transport numbers must be finite")
        return
    if type(value) not in (list, dict):
        raise TypeError("event transport requires exact portable JSON containers/scalars")
    active = set() if active is None else active
    if id(value) in active:
        raise ValueError("event transport cannot be cyclic")
    active.add(id(value))
    try:
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise TypeError("event transport object keys must be strings")
            children = value.values()
        else:
            children = value
        for child in children:
            _json_value(child, active)
    finally:
        active.remove(id(value))


def _wire(value: Any) -> Any:
    if type(value) is int:
        return ["int", hex(value)]
    if type(value) is list:
        return ["list", [_wire(item) for item in value]]
    if type(value) is dict:
        return ["dict", [[key, _wire(value[key])] for key in sorted(value)]]
    return ["scalar", value]


def _unwire(value: Any) -> Any:
    if type(value) is not list or len(value) != 2:
        raise ValueError("invalid event byte encoding")
    kind, content = value
    match kind:
        case "int":
            if type(content) is not str:
                raise ValueError("invalid event integer")
            result = int(content, 16)
            if hex(result) != content:
                raise ValueError("noncanonical event integer")
            return result
        case "list":
            return [_unwire(item) for item in content]
        case "dict":
            result = {}
            for key, item in content:
                if type(key) is not str or key in result:
                    raise ValueError("invalid event object key")
                result[key] = _unwire(item)
            return result
        case "scalar":
            if content is not None and type(content) not in (str, bool, float):
                raise ValueError("invalid event scalar")
            _json_value(content)
            return content
        case _:
            raise ValueError("unknown event byte encoding")


def decode_event_bytes(data: bytes) -> Any:
    """Decode internal tagged JSON bytes; large integers never use decimal conversion."""
    return _unwire(json.loads(data))


def event_bytes(value: Any) -> bytes:
    """Canonical encoded transport, never pickle/repr fallback or arbitrary host objects."""
    _json_value(value)
    return json.dumps(
        _wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def event_id(invocation_id: str) -> str:
    validate_invocation_id(invocation_id)
    return hashlib.sha256(event_bytes(["togeari.event", 1, invocation_id])).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class CommittedEvent:
    job_execution_id: str
    sequence: int
    invocation_id: str
    topic: str
    event_id: str
    _payload: bytes
    _origin: bytes
    _encoded: bytes

    def __init__(
        self,
        job_execution_id: str,
        sequence: int,
        invocation_id: str,
        topic: str,
        encoded_payload: Any,
        origin: Any = None,
    ):
        if hasattr(self, "_encoded"):
            raise TypeError("immutable CommittedEvent cannot be reinitialized")
        validate_job_execution_id(job_execution_id)
        validate_invocation_id(invocation_id)
        if type(sequence) is not int or not 0 <= sequence < (1 << 63):
            raise ValueError("event sequence must be a nonnegative signed-i64 integer")
        if type(topic) is not str:
            raise TypeError("event topic must be an exact string")
        payload, source = event_bytes(encoded_payload), event_bytes(origin)
        eid = event_id(invocation_id)
        envelope = {
            "schema": "togeari.committed_event",
            "version": 1,
            "job_execution_id": job_execution_id,
            "sequence": sequence,
            "event_id": eid,
            "invocation_id": invocation_id,
            "topic": topic,
            "encoded_payload": decode_event_bytes(payload),
            "origin": decode_event_bytes(source),
        }
        for name, value in (
            ("job_execution_id", job_execution_id),
            ("sequence", sequence),
            ("invocation_id", invocation_id),
            ("topic", topic),
            ("event_id", eid),
            ("_payload", payload),
            ("_origin", source),
            ("_encoded", event_bytes(envelope)),
        ):
            object.__setattr__(self, name, value)

    @property
    def encoded_payload(self) -> Any:
        return decode_event_bytes(self._payload)

    @property
    def origin(self) -> Any:
        return decode_event_bytes(self._origin)

    @property
    def size_bytes(self) -> int:
        return len(self._encoded)

    def encoded_bytes(self) -> bytes:
        return self._encoded

    def to_dict(self) -> dict:
        return decode_event_bytes(self._encoded)

    def semantic_bytes(self) -> bytes:
        envelope = self.to_dict()
        envelope.pop("origin")
        return event_bytes(envelope)

    def __setstate__(self, state):
        raise TypeError("immutable CommittedEvent state cannot be replaced")

    def __reduce__(self):
        return (
            type(self),
            (
                self.job_execution_id,
                self.sequence,
                self.invocation_id,
                self.topic,
                self.encoded_payload,
                self.origin,
            ),
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "CommittedEvent":
        return cls.from_dict(decode_event_bytes(data))

    @classmethod
    def from_dict(cls, data: dict) -> "CommittedEvent":
        expected = {
            "schema",
            "version",
            "job_execution_id",
            "sequence",
            "event_id",
            "invocation_id",
            "topic",
            "encoded_payload",
            "origin",
        }
        if type(data) is not dict or set(data) != expected:
            raise ValueError("invalid committed event envelope")
        if (
            data["schema"] != "togeari.committed_event"
            or type(data["version"]) is not int
            or data["version"] != 1
        ):
            raise ValueError("unsupported committed event version")
        event = cls(
            data["job_execution_id"],
            data["sequence"],
            data["invocation_id"],
            data["topic"],
            data["encoded_payload"],
            data["origin"],
        )
        if data["event_id"] != event.event_id:
            raise ValueError("committed event ID differs from invocation")
        return event


@runtime_checkable
class EventSink(Protocol):
    def write_event(self, event: CommittedEvent) -> None:
        """Return only on accepted delivery; raising leaves the event pending for retry."""

    def close(self) -> None: ...


@runtime_checkable
class EventJournal(Protocol):
    def append(self, event: CommittedEvent) -> None:
        """Atomically commit idempotently by ID/content and contiguous per-job sequence."""

    def pending(self, limit: int = 64, max_bytes: int | None = None) -> list[CommittedEvent]:
        """Bounded unacknowledged events in commit order, preserving per-job order."""

    def ack(self, event_id: str) -> None:
        """Idempotently mark delivery; retain identity/sequence commitment for conflicts."""

    def frontier(self, job_execution_id: str) -> int:
        """Highest committed sequence, -1 before any append. Acknowledgment does not rewind it."""

    def identity(self) -> dict:
        """Cached detached portable journal descriptor; never a machine-local path fingerprint."""

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True, init=False)
class EventOutput:
    sink: EventSink
    journal: EventJournal
    max_events: int = 64
    max_bytes: int = 8 << 20
    retry_s: float = 0.05
    close_timeout_s: float = 5.0
    sink_revision: str | None = None
    _descriptor: bytes = field(init=False, repr=False)

    def __init__(
        self,
        sink,
        journal,
        max_events=64,
        max_bytes=8 << 20,
        retry_s=0.05,
        close_timeout_s=5.0,
        sink_revision=None,
    ):
        if hasattr(self, "_descriptor"):
            raise TypeError("immutable EventOutput cannot be reinitialized")
        for name, value in (
            ("sink", sink),
            ("journal", journal),
            ("max_events", max_events),
            ("max_bytes", max_bytes),
            ("retry_s", retry_s),
            ("close_timeout_s", close_timeout_s),
            ("sink_revision", sink_revision),
        ):
            object.__setattr__(self, name, value)
        for target, methods in (
            (self.sink, ("write_event", "close")),
            (self.journal, ("append", "pending", "ack", "frontier", "identity", "close")),
        ):
            if target is None or any(not callable(getattr(target, name, None)) for name in methods):
                raise TypeError("EventOutput requires both an EventSink and EventJournal")
        for name in ("max_events", "max_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("retry_s", "close_timeout_s"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.retry_s == 0:
            raise ValueError("retry_s must be positive to avoid a delivery busy loop")
        if self.sink_revision is not None and (
            type(self.sink_revision) is not str or not self.sink_revision
        ):
            raise ValueError("sink_revision must be a nonempty string or None")

        descriptor = {
            "schema": "togeari.event_output",
            "version": 1,
            "journal": self.journal.identity(),
            "max_bytes": self.max_bytes,
            "sink_revision": self.sink_revision,
            "delivery": "at-least-once/1",
            "oversized": "reject",
            "commit": "journal-append-ack/1",
        }
        object.__setattr__(self, "_descriptor", event_bytes(descriptor))

    def __setstate__(self, state):
        raise TypeError("immutable EventOutput state cannot be replaced")

    def __reduce__(self):
        return (
            type(self),
            (
                self.sink,
                self.journal,
                self.max_events,
                self.max_bytes,
                self.retry_s,
                self.close_timeout_s,
                self.sink_revision,
            ),
        )

    def descriptor(self) -> dict:
        """Cached detached identity; never perform journal I/O during dispatch/inspection."""
        return decode_event_bytes(self._descriptor)
