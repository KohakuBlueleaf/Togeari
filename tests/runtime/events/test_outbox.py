"""Outbox acceptance, durability, recovery, bounds, and retry without scheduler timing races."""

import queue
import threading
from types import SimpleNamespace

import pytest

from togeari.core.contracts.events import CommittedEvent, EventOutput, EventTooLarge
from togeari.core.runtime.events import EventOutboxManager
from togeari.io.events import MemoryEventJournal, MemoryEventSink, SQLiteEventJournal

UID = "1" * 32


class Dispatcher:
    def __init__(self):
        self.inbox = queue.Queue()

    def until(self, predicate):
        for _ in range(1000):
            if predicate():
                return
            kind, callback = self.inbox.get(timeout=5)
            assert kind == "call"
            callback()
        raise AssertionError("outbox did not reach expected state")


class EncodedEvent:
    """The KIR.Event public transport seam, without coupling worker tests to its codec."""

    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return {"topic": "test", "payload": self.payload}


def event(sequence=0, uid=UID, value=None):
    return CommittedEvent(uid, sequence, f"{sequence + int(uid[0], 16):064x}", "test", value)


@pytest.fixture
def outbox():
    managers = []

    def make(journal=None, sink=None, **limits):
        journal = MemoryEventJournal() if journal is None else journal
        sink = MemoryEventSink() if sink is None else sink
        host = SimpleNamespace(dispatcher=Dispatcher())
        output = EventOutput(sink, journal, retry_s=0.001, close_timeout_s=0.05, **limits)
        manager = EventOutboxManager(host, output)
        managers.append(manager)
        manager.start()
        return manager, host.dispatcher, journal, sink

    yield make
    for manager in managers:
        manager.close(timeout=0)


class HeldJournal(MemoryEventJournal):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.worker = None

    def append(self, value):
        self.worker = threading.get_ident()
        self.entered.set()
        assert self.release.wait(5)
        super().append(value)


class HeldSink(MemoryEventSink):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write_event(self, value):
        self.entered.set()
        assert self.release.wait(5)
        super().write_event(value)


def ready(manager, dispatcher):
    dispatcher.until(lambda: not manager.status()["recovering"])


def test_append_ack_permits_retirement_not_before_and_sink_waits_for_retired(outbox):
    journal = HeldJournal()
    manager, dispatcher, _, sink = outbox(journal=journal)
    ready(manager, dispatcher)
    value = event()
    woke = []
    token = manager.commit(value, lambda: woke.append("append"))
    try:
        assert journal.entered.wait(5)
        assert journal.worker != threading.get_ident()
        assert token["status"] == "pending" and manager.pending(UID)
        assert sink.events == [] and woke == []
        with pytest.raises(RuntimeError, match="before journal"):
            manager.retired(value)
    finally:
        journal.release.set()
    dispatcher.until(lambda: token["status"] == "committed")
    assert woke == ["append"] and not manager.pending(UID) and sink.events == []
    manager.retired(value)
    dispatcher.until(lambda: manager.status()["pending_events"] == 0)
    assert sink.events == [value] and journal.pending() == [] and journal.frontier(UID) == 0
    assert manager.close()["status"] == "closed"


@pytest.mark.parametrize("bound", ["count", "bytes"])
def test_capacity_is_held_until_sink_and_journal_ack(outbox, bound):
    first, second = event(0), event(1)
    sink = HeldSink()
    limits = {"max_events": 1} if bound == "count" else {"max_bytes": first.size_bytes}
    manager, dispatcher, journal, _ = outbox(sink=sink, **limits)
    ready(manager, dispatcher)
    token = manager.commit(first, lambda: None)
    dispatcher.until(lambda: token["status"] == "committed")
    manager.retired(first)
    woke = []
    try:
        assert sink.entered.wait(5)
        rejected = manager.commit(second, lambda: woke.append("capacity"))
        assert rejected["status"] == "full"
        assert manager.status()["pending_events"] == 1
        assert manager.status()["pending_bytes"] == first.size_bytes
        assert journal.frontier(UID) == 0 and woke == []
    finally:
        sink.release.set()
    dispatcher.until(lambda: bool(woke))
    token = manager.commit(second, lambda: None)
    dispatcher.until(lambda: token["status"] == "committed")
    manager.retired(second)
    dispatcher.until(lambda: manager.status()["pending_events"] == 0)
    assert [item.sequence for item in sink.events] == [0, 1]


def test_prepare_captures_payload_and_rejects_oversized_before_reservation(outbox):
    manager, dispatcher, journal, _ = outbox(max_bytes=1024)
    ready(manager, dispatcher)
    raw = {"items": [1]}
    prepared = manager.prepare(UID, 0, "a" * 64, EncodedEvent(raw))
    raw["items"].append(2)
    assert prepared.encoded_payload == {"items": [1]}
    with pytest.raises(EventTooLarge):
        manager.prepare(UID, 0, "b" * 64, EncodedEvent("x" * 2048))
    assert manager.status()["pending_events"] == 0 and journal.frontier(UID) == -1


class LostAppendReply(MemoryEventJournal):
    def __init__(self):
        super().__init__()
        self.calls = []

    def append(self, value):
        self.calls.append(value.event_id)
        super().append(value)
        if len(self.calls) == 1:
            raise OSError("commit succeeded but acknowledgement was lost")


class LostDeliveryReply(MemoryEventSink):
    def write_event(self, value):
        super().write_event(value)
        if len(self.events) == 1:
            raise OSError("consumer committed but acknowledgement was lost")


def test_retry_preserves_same_event_id_after_ambiguous_append_and_delivery(outbox):
    journal, sink = LostAppendReply(), LostDeliveryReply()
    manager, dispatcher, _, _ = outbox(journal=journal, sink=sink)
    ready(manager, dispatcher)
    value = event()
    token = manager.commit(value, lambda: None)
    dispatcher.until(lambda: token["status"] == "committed")
    assert journal.calls == [value.event_id, value.event_id]
    assert manager.status()["committed"] == 1
    manager.retired(value)
    dispatcher.until(lambda: manager.status()["pending_events"] == 0)
    assert sink.events == [value, value]
    assert journal.frontier(UID) == 0 and journal.pending() == []


def test_per_job_delivery_order_when_later_retirement_notification_arrives_first(outbox):
    manager, dispatcher, _, sink = outbox()
    ready(manager, dispatcher)
    first, second = event(0), event(1)
    one, two = manager.commit(first, lambda: None), manager.commit(second, lambda: None)
    dispatcher.until(lambda: one["status"] == two["status"] == "committed")
    manager.retired(second)
    assert sink.events == []
    manager.retired(first)
    dispatcher.until(lambda: manager.status()["pending_events"] == 0)
    assert sink.events == [first, second]


@pytest.mark.parametrize("persistent", [False, True])
def test_recovery_redelivers_committed_unretired_events_bounded_pages(outbox, tmp_path, persistent):
    path = tmp_path / "events.sqlite"
    journal = SQLiteEventJournal(path) if persistent else MemoryEventJournal()
    values = [event(index) for index in range(3)]
    for value in values:
        journal.append(value)
    if persistent:
        identity = journal.identity()
        journal.close()
        journal = SQLiteEventJournal(path)
        assert journal.identity() == identity
    manager, dispatcher, _, sink = outbox(journal=journal, max_events=1, max_bytes=1024)
    dispatcher.until(lambda: not manager.status()["recovering"])
    assert sink.events == values and manager.status()["pending_events"] == 0
    assert journal.pending() == [] and journal.frontier(UID) == 2


def test_restore_frontier_is_checked_off_dispatcher_and_rejects_stale_snapshot(outbox):
    manager, dispatcher, journal, _ = outbox()
    ready(manager, dispatcher)
    state = {
        "version": 1,
        "next_sequence": 0,
        "committed_watermark": -1,
        "journal_identity": journal.identity(),
    }
    seen = []
    manager.check_frontier(UID, state, seen.append)
    assert manager.pending_restores == 1 and seen == []
    dispatcher.until(lambda: bool(seen))
    assert seen.pop()["allow"] and manager.pending_restores == 0
    value = event()
    token = manager.commit(value, lambda: None)
    dispatcher.until(lambda: token["status"] == "committed")
    manager.check_frontier(UID, state, seen.append)
    dispatcher.until(lambda: bool(seen))
    assert not seen.pop()["allow"]
    state.update(next_sequence=1, committed_watermark=0)
    manager.check_frontier(UID, state, seen.append)
    dispatcher.until(lambda: bool(seen))
    assert seen.pop()["allow"]
    state["journal_identity"] = {"wrong": True}
    manager.check_frontier(UID, state, seen.append)
    assert not seen.pop()["allow"]


def test_shutdown_unknown_append_is_not_claimed_absent_and_workers_are_daemons(outbox):
    journal = HeldJournal()
    manager, dispatcher, _, _ = outbox(journal=journal)
    ready(manager, dispatcher)
    value = event()
    token = manager.commit(value, lambda: None)
    try:
        assert journal.entered.wait(5)
        report = manager.close(timeout=0)
        assert report["status"] == "pending" and report["unknown_appends"] == [value.event_id]
        assert token["status"] == "unknown" and all(t.daemon for t in manager._threads)
    finally:
        journal.release.set()
    for thread in manager._threads:
        thread.join(5)
        assert not thread.is_alive()
    assert journal.frontier(UID) == 0  # late commitment is durable, never silently erased


def test_force_unknown_freezes_token_and_late_ack_cannot_wake_or_publish(outbox):
    journal = HeldJournal()
    manager, dispatcher, _, sink = outbox(journal=journal)
    ready(manager, dispatcher)
    value = event()
    wakes = []
    token = manager.commit(value, lambda: wakes.append("commit"))
    try:
        assert journal.entered.wait(5)
        assert manager.force_unknown() == [{"job_execution_id": UID, "event_id": value.event_id}]
        assert token["status"] == "unknown"
    finally:
        journal.release.set()
    dispatcher.until(lambda: "late_persistence" in token)
    assert token["status"] == "unknown" and token["late_persistence"] == "committed"
    assert wakes == [] and sink.events == []
    assert journal.pending() == [value]
    assert manager.close(timeout=0)["unknown_appends"] == [value.event_id]


class CachedIdentityJournal(MemoryEventJournal):
    def __init__(self):
        super().__init__()
        self.identities = 0
        self.closed = False

    def identity(self):
        self.identities += 1
        return super().identity()

    def close(self):
        self.closed = True


def test_manager_uses_cached_descriptor_and_never_closes_shared_journal(outbox):
    journal = CachedIdentityJournal()
    manager, dispatcher, _, _ = outbox(journal=journal)
    ready(manager, dispatcher)
    assert journal.identities == 1
    manager.configuration()
    manager.status()
    first = event()
    other_origin = CommittedEvent(UID, 0, first.invocation_id, "test", None, {"line": 99})
    token = manager.commit(first, lambda: None)
    assert manager.commit(other_origin, lambda: None) is token
    dispatcher.until(lambda: token["status"] == "committed")
    manager.retired(first)
    dispatcher.until(lambda: manager.status()["pending_events"] == 0)
    assert manager.close()["status"] == "closed"
    assert not journal.closed and journal.identities == 1


class LostAck(MemoryEventJournal):
    def __init__(self):
        super().__init__()
        self.acks = []

    def ack(self, eid):
        self.acks.append(eid)
        super().ack(eid)
        if len(self.acks) == 1:
            raise OSError("journal ack reply lost")


def test_ack_retry_does_not_allocate_another_event_or_redeliver_sink(outbox):
    journal = LostAck()
    manager, dispatcher, _, sink = outbox(journal=journal)
    ready(manager, dispatcher)
    value = event()
    token = manager.commit(value, lambda: None)
    dispatcher.until(lambda: token["status"] == "committed")
    manager.retired(value)
    dispatcher.until(lambda: manager.status()["pending_events"] == 0)
    assert journal.acks == [value.event_id, value.event_id]
    assert sink.events == [value] and manager.status()["delivered"] == 1


class GeneratorJournal(MemoryEventJournal):
    def pending(self, limit=64, max_bytes=None):
        def invalid():
            raise AssertionError("outbox must not consume an unbounded pending iterator")
            yield

        return invalid()


def test_recovery_rejects_nonlist_without_consuming_unbounded_provider(outbox):
    manager, dispatcher, _, _ = outbox(journal=GeneratorJournal())
    ready(manager, dispatcher)
    assert "bounded list" in manager.status()["recovery_error"]
    assert manager.commit(event(), lambda: None)["status"] == "failed"
    assert manager.close()["status"] == "pending"


class HeldFrontier(MemoryEventJournal):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def frontier(self, uid):
        self.entered.set()
        assert self.release.wait(5)
        return super().frontier(uid)


@pytest.mark.parametrize("cancel_on_dispatcher", [False, True])
def test_shutdown_clears_frontier_callbacks_and_late_results_cannot_reenter_core(
    outbox, cancel_on_dispatcher
):
    journal = HeldFrontier()
    manager, dispatcher, _, _ = outbox(journal=journal)
    ready(manager, dispatcher)
    state = {
        "version": 1,
        "next_sequence": 0,
        "committed_watermark": -1,
        "journal_identity": journal.identity(),
    }
    decisions = []
    manager.check_frontier(UID, state, decisions.append)
    try:
        assert journal.entered.wait(5)
        if cancel_on_dispatcher:
            manager.cancel_restores()
            assert len(decisions) == 1 and not decisions[0]["allow"]
        report = manager.close(timeout=0)
        assert manager.pending_restores == 0 and not manager._restores
        assert report["cancelled_restores"] == (0 if cancel_on_dispatcher else 1)
    finally:
        journal.release.set()
    for thread in manager._threads:
        thread.join(5)
        assert not thread.is_alive()
    assert len(decisions) == (1 if cancel_on_dispatcher else 0)
    assert manager._completions.empty()


def test_one_throwing_restore_callback_cannot_prevent_remaining_shutdown_replies(outbox):
    journal = HeldFrontier()
    manager, dispatcher, _, _ = outbox(journal=journal)
    ready(manager, dispatcher)
    state = {
        "version": 1,
        "next_sequence": 0,
        "committed_watermark": -1,
        "journal_identity": journal.identity(),
    }
    decisions = []

    def bad_callback(decision):
        raise RuntimeError("staged admission callback failed")

    manager.check_frontier(UID, state, bad_callback)
    manager.check_frontier("2" * 32, state, decisions.append)
    try:
        assert journal.entered.wait(5)
        manager.cancel_restores()
        assert len(decisions) == 1 and not decisions[0]["allow"]
        assert manager.pending_restores == 0 and not manager._restores
        assert manager.status()["callback_errors"] == 1
        manager.close(timeout=0)
    finally:
        journal.release.set()
    for thread in manager._threads:
        thread.join(5)
        assert not thread.is_alive()
    assert manager.pending_restores == 0 and len(decisions) == 1


def test_close_discards_already_queued_frontier_completion_before_draining(outbox):
    manager, dispatcher, journal, _ = outbox()
    ready(manager, dispatcher)
    state = {
        "version": 1,
        "next_sequence": 0,
        "committed_watermark": -1,
        "journal_identity": journal.identity(),
    }
    decisions = []
    manager.check_frontier(UID, state, decisions.append)
    kind, deferred = dispatcher.inbox.get(timeout=5)
    assert kind == "call" and decisions == []
    report = manager.close(timeout=0)
    deferred()  # already queued dispatcher callback must also be harmless after close
    assert report["cancelled_restores"] == 1
    assert decisions == [] and manager.pending_restores == 0


def test_force_unknown_also_disables_capacity_wakes_from_another_acked_event(outbox):
    sink = HeldSink()
    manager, dispatcher, _, _ = outbox(sink=sink, max_events=1)
    ready(manager, dispatcher)
    first, second = event(), event(uid="2" * 32)
    token = manager.commit(first, lambda: None)
    dispatcher.until(lambda: token["status"] == "committed")
    manager.retired(first)
    wakes = []
    try:
        assert sink.entered.wait(5)
        assert manager.commit(second, lambda: wakes.append("capacity"))["status"] == "full"
        assert manager.force_unknown() == []
    finally:
        sink.release.set()
    dispatcher.until(lambda: manager.status()["pending_events"] == 0)
    assert wakes == [] and not manager._waiters


def test_no_output_is_lazy_and_no_workers_start():
    manager = EventOutboxManager(SimpleNamespace(dispatcher=Dispatcher()))
    manager.start()
    assert manager._threads == [] and not manager.configuration()["enabled"]
    assert manager.close()["status"] == "closed"
