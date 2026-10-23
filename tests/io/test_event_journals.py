"""Outbox append-before-ack recovery and idempotence for memory and SQLite journals."""

import threading

import pytest

from togeari import CommittedEvent
from togeari.core.contracts.events import EventConflict, EventTooLarge
from togeari.io import MemoryEventJournal, MemoryEventSink, SQLiteEventJournal

JOB = "a" * 32


def event(sequence=0, payload=None, invocation=None):
    return CommittedEvent(JOB, sequence, invocation or f"{sequence + 1:064x}", "topic", payload)


@pytest.fixture(params=["memory", "sqlite"])
def journal(request, tmp_path):
    result = (
        MemoryEventJournal()
        if request.param == "memory"
        else SQLiteEventJournal(tmp_path / "events.db")
    )
    yield result
    result.close()


def test_append_idempotent_conflicts_and_contiguous_sequence(journal):
    assert journal.frontier(JOB) == -1
    first = event(payload={"x": 1})
    journal.append(first)
    journal.append(first)
    assert journal.pending() == [first]
    assert journal.frontier(JOB) == 0
    with pytest.raises(EventConflict):
        journal.append(event(payload={"x": 2}))
    with pytest.raises(EventConflict):
        journal.append(event(0, invocation="f" * 64))
    with pytest.raises(EventConflict):
        journal.append(event(2))
    assert journal.frontier(JOB) == 0
    journal.append(event(1))
    assert journal.frontier(JOB) == 1
    assert journal.pending(1) == [first]


def test_origin_is_not_semantic_conflict_and_large_integer_roundtrips(journal):
    first = event(payload={"huge": 10**5000})
    relocated = CommittedEvent(
        JOB,
        0,
        first.invocation_id,
        first.topic,
        first.encoded_payload,
        {"module": "moved", "line": 3},
    )
    journal.append(first)
    journal.append(relocated)
    assert journal.pending() == [first]
    assert journal.pending()[0].encoded_payload == {"huge": 10**5000}


def test_pending_byte_limit_never_skips_oversize_or_exceeds_page(journal):
    first, second = event(payload="a"), event(1, payload="b" * 100)
    journal.append(first)
    journal.append(second)
    assert journal.pending(limit=10, max_bytes=first.size_bytes) == [first]
    assert journal.pending(limit=10, max_bytes=first.size_bytes + second.size_bytes) == [
        first,
        second,
    ]
    with pytest.raises(EventTooLarge):
        journal.pending(limit=10, max_bytes=first.size_bytes - 1)
    journal.ack(first.event_id)
    with pytest.raises(EventTooLarge):
        journal.pending(limit=10, max_bytes=first.size_bytes)


def test_ack_does_not_forget_frontier_or_duplicate_identity(journal):
    first, second = event(), event(1)
    journal.append(first)
    journal.append(second)
    journal.ack(first.event_id)
    journal.ack(first.event_id)
    journal.append(first)
    assert journal.pending() == [second]
    assert journal.frontier(JOB) == 1
    with pytest.raises(EventConflict):
        journal.append(event(payload="different"))
    with pytest.raises(KeyError):
        journal.ack("f" * 64)


def test_concurrent_identical_append_commits_once(journal):
    first = event()
    errors = []

    def append():
        try:
            journal.append(first)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=append) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert errors == []
    assert journal.pending() == [first]
    assert journal.frontier(JOB) == 0


def test_sqlite_reopen_retains_unacked_frontier_identity_and_delivery_retry(tmp_path):
    path = tmp_path / "events.db"
    first, second = event(payload=[1]), event(1, payload=[2])
    with SQLiteEventJournal(path) as journal:
        identity = journal.identity()
        journal.append(first)
        journal.append(second)
        journal.ack(first.event_id)
    sink = MemoryEventSink()
    with SQLiteEventJournal(path) as recovered:
        assert recovered.identity() == identity
        assert identity["durable"] is True
        assert recovered.frontier(JOB) == 1
        assert recovered.pending() == [second]
        # Crash-equivalent gap between sink success and acknowledgment: redelivery is expected.
        sink.write_event(second)
    with SQLiteEventJournal(path) as recovered:
        sink.write_event(recovered.pending()[0])
        recovered.ack(second.event_id)
        assert recovered.pending() == []
        assert recovered.frontier(JOB) == 1
    assert [e.event_id for e in sink.events] == [second.event_id, second.event_id]


def test_sqlite_separate_connections_serialize_conflicts(tmp_path):
    path = tmp_path / "shared.db"
    with SQLiteEventJournal(path) as first, SQLiteEventJournal(path) as second:
        assert first.identity() == second.identity()
        first.append(event())
        second.append(event())
        with pytest.raises(EventConflict):
            second.append(event(payload="conflict"))
        second.append(event(1))
        assert first.frontier(JOB) == 1


def test_memory_identity_is_explicitly_nondurable_and_detached():
    first, second = MemoryEventJournal(), MemoryEventJournal()
    assert first.identity()["durable"] is False
    assert first.identity() != second.identity()
    changed = first.identity()
    changed["journal_id"] = "wrong"
    assert first.identity() != changed


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_pending_requires_bounded_positive_limit(journal, limit):
    with pytest.raises(ValueError):
        journal.pending(limit)
