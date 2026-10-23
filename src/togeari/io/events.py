"""Explicit event sinks and idempotent outbox journals.

SQLite commits before append returns (WAL, synchronous FULL). Delivery acknowledgments
are separate transactions. Reopening preserves journal identity, pending events and frontier.
Memory implementations are deliberately nondurable. Consumers still need idempotency.
"""

import sqlite3
import threading
import uuid
from pathlib import Path

from togeari.core.contracts.events import CommittedEvent, EventConflict, EventTooLarge
from togeari.core.contracts.identity import validate_invocation_id, validate_job_execution_id


def _limit(limit, max_bytes=None):
    if type(limit) is not int or limit < 1:
        raise ValueError("pending limit must be a positive integer")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise ValueError("pending max_bytes must be a positive integer or None")


def _page(events, limit, max_bytes):
    result, total = [], 0
    for event in events:
        if max_bytes is not None and total + event.size_bytes > max_bytes:
            if not result:
                raise EventTooLarge("first pending event exceeds recovery byte capacity")
            break
        result.append(event)
        total += event.size_bytes
        if len(result) == limit:
            break
    return result


def _event(event):
    if type(event) is not CommittedEvent:
        raise TypeError("journal requires an exact immutable CommittedEvent")


class MemoryEventSink:
    """Thread-safe delivery collection; duplicates intentionally expose at-least-once delivery."""

    def __init__(self):
        self._events = []
        self._lock = threading.Lock()

    def write_event(self, event: CommittedEvent) -> None:
        _event(event)
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> list[CommittedEvent]:
        with self._lock:
            return list(self._events)

    def close(self) -> None:
        pass


class MemoryEventJournal:
    """Nondurable journal with the same identity/conflict/sequence rules as SQLite."""

    def __init__(self):
        self._id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._events = {}
        self._pending = {}
        self._frontiers = {}

    def identity(self) -> dict:
        return {
            "schema": "togeari.event_journal",
            "version": 1,
            "kind": "memory",
            "journal_id": self._id,
            "durable": False,
        }

    def append(self, event: CommittedEvent) -> None:
        _event(event)
        with self._lock:
            old = self._events.get(event.event_id)
            if old is not None:
                if old.semantic_bytes() != event.semantic_bytes():
                    raise EventConflict("event ID already committed with different content")
                return
            expected = self._frontiers.get(event.job_execution_id, -1) + 1
            if event.sequence != expected:
                raise EventConflict("event sequence must follow the committed job frontier")
            self._events[event.event_id] = event
            self._pending[event.event_id] = event
            self._frontiers[event.job_execution_id] = event.sequence

    def pending(self, limit: int = 64, max_bytes: int | None = None) -> list[CommittedEvent]:
        _limit(limit, max_bytes)
        with self._lock:
            return _page(self._pending.values(), limit, max_bytes)

    def ack(self, event_id: str) -> None:
        validate_invocation_id(event_id)
        with self._lock:
            if event_id not in self._events:
                raise KeyError("cannot acknowledge an uncommitted event")
            self._pending.pop(event_id, None)

    def frontier(self, job_execution_id: str) -> int:
        validate_job_execution_id(job_execution_id)
        with self._lock:
            return self._frontiers.get(job_execution_id, -1)

    def close(self) -> None:
        # No worker/handle to close; the caller may share the journal across machines.
        pass


class SQLiteEventJournal:
    """Durable transactional journal; open the same path to recover unacknowledged events.

    Concurrent writers get SQLite serialization and sequence conflicts, not ownership leases.
    External sink delivery is never inside this database transaction.
    """

    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("use MemoryEventJournal for explicitly nondurable event storage")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        db = self._connection
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS event_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS committed_events "
                "(ordinal INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, "
                "job_id TEXT NOT NULL, sequence INTEGER NOT NULL, envelope BLOB NOT NULL, "
                "acknowledged INTEGER NOT NULL DEFAULT 0, UNIQUE(job_id,sequence))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS event_frontiers "
                "(job_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS event_pending ON committed_events(ordinal) "
                "WHERE acknowledged=0"
            )
            db.execute("INSERT OR IGNORE INTO event_metadata VALUES ('version','1')")
            db.execute(
                "INSERT OR IGNORE INTO event_metadata VALUES ('journal_id',?)", (uuid.uuid4().hex,)
            )
            metadata = dict(db.execute("SELECT key,value FROM event_metadata"))
            if metadata.get("version") != "1":
                raise ValueError("unsupported event journal version")
            self._id = validate_job_execution_id(metadata["journal_id"])
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.close()
            raise

    def identity(self) -> dict:
        return {
            "schema": "togeari.event_journal",
            "version": 1,
            "kind": "sqlite",
            "journal_id": self._id,
            "durable": True,
        }

    def append(self, event: CommittedEvent) -> None:
        _event(event)
        encoded = event.encoded_bytes()
        with self._lock:
            db = self._connection
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute(
                    "SELECT envelope FROM committed_events WHERE event_id=?", (event.event_id,)
                ).fetchone()
                if old is not None:
                    if (
                        CommittedEvent.from_bytes(bytes(old[0])).semantic_bytes()
                        != event.semantic_bytes()
                    ):
                        raise EventConflict("event ID already committed with different content")
                else:
                    row = db.execute(
                        "SELECT sequence FROM event_frontiers WHERE job_id=?",
                        (event.job_execution_id,),
                    ).fetchone()
                    expected = 0 if row is None else row[0] + 1
                    if event.sequence != expected:
                        raise EventConflict("event sequence must follow the committed job frontier")
                    db.execute(
                        "INSERT INTO committed_events(event_id,job_id,sequence,envelope) "
                        "VALUES (?,?,?,?)",
                        (event.event_id, event.job_execution_id, event.sequence, encoded),
                    )
                    db.execute(
                        "INSERT INTO event_frontiers(job_id,sequence) VALUES (?,?) "
                        "ON CONFLICT(job_id) DO UPDATE SET sequence=excluded.sequence",
                        (event.job_execution_id, event.sequence),
                    )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def pending(self, limit: int = 64, max_bytes: int | None = None) -> list[CommittedEvent]:
        _limit(limit, max_bytes)
        with self._lock:
            # Read byte lengths before materializing envelopes, including the first oversize.
            rows = self._connection.execute(
                "SELECT ordinal,length(envelope) FROM committed_events WHERE acknowledged=0 "
                "ORDER BY ordinal LIMIT ?",
                (limit,),
            )
            result, total = [], 0
            for ordinal, size in rows:
                if max_bytes is not None and total + size > max_bytes:
                    if not result:
                        raise EventTooLarge("first pending event exceeds recovery byte capacity")
                    break
                data = self._connection.execute(
                    "SELECT envelope FROM committed_events WHERE ordinal=?", (ordinal,)
                ).fetchone()[0]
                result.append(CommittedEvent.from_bytes(data))
                total += size
            return result

    def ack(self, event_id: str) -> None:
        validate_invocation_id(event_id)
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE committed_events SET acknowledged=1 WHERE event_id=?", (event_id,)
            )
            if cursor.rowcount == 0:
                raise KeyError("cannot acknowledge an uncommitted event")

    def frontier(self, job_execution_id: str) -> int:
        validate_job_execution_id(job_execution_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT sequence FROM event_frontiers WHERE job_id=?", (job_execution_id,)
            ).fetchone()
            return -1 if row is None else row[0]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
