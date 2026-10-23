"""Core event cursor/retirement contracts independent of consumer implementations."""

import pickle

import pytest
from kohakunode.events import Event

from togeari import EventOutput, Machine
from togeari.core.contracts.digest import digest
from togeari.core.contracts.events import CommittedEvent
from togeari.core.runtime.retire import snapshot
from togeari.core.runtime.retire.codec import encode_snapshot, prepare_restore
from togeari.core.runtime.state import Job
from togeari.io import MemoryEventJournal, MemoryEventSink
from togeari.stores import MemoryStore

SOURCE = '@def ()main():\n    ("test", {"value": 1})emit()\n'


@pytest.fixture
def event_machine():
    journal, sink = MemoryEventJournal(), MemoryEventSink()
    machine = Machine(
        program=SOURCE,
        entry="main",
        core="python",
        store=MemoryStore(),
        snapshot_every=1,
        record=1.0,
        event_output=EventOutput(sink, journal, sink_revision="test/1"),
    )
    yield machine, journal, sink
    machine.stop()


def initial(machine, uid):
    entry = machine.code.entries["main"]
    job = Job("job", entry, {})
    return {
        "job_execution_id": uid,
        "pc": entry.addr,
        "prefix": (),
        "counter": 0,
        "frames": [],
        "values": {},
        "stats": {**job.stats, "committed_events": {"version": 1, "events": []}},
        "steps": 0,
        "record": [],
        "event_state": {
            "version": 1,
            "next_sequence": 0,
            "committed_watermark": -1,
            "journal_identity": machine.event_output.descriptor()["journal"],
        },
    }


def test_snapshot_emit_cursor_required(event_machine):
    machine, _, _ = event_machine
    entry = machine.code.entries["main"]
    state = initial(machine, "1" * 32)
    blob = encode_snapshot(state, entry, machine.code, entry.region.execution)
    envelope = pickle.loads(blob)
    del envelope["state"]["event_state"]
    result = prepare_restore(
        pickle.dumps(envelope), entry, machine.code, entry.region.execution, "strict"
    )
    assert not result["decision"]["allow"]
    assert result["decision"]["issues"][0]["path"] == "state.event_state"


@pytest.mark.parametrize(
    "field,value",
    [
        ("next_sequence", True),
        ("next_sequence", 2),
        ("committed_watermark", -2),
        ("version", True),
        ("journal_identity", {"bad": object()}),
    ],
)
def test_snapshot_malformed_event_cursor_rejects(event_machine, field, value):
    machine, _, _ = event_machine
    entry = machine.code.entries["main"]
    blob = encode_snapshot(initial(machine, "1" * 32), entry, machine.code, entry.region.execution)
    envelope = pickle.loads(blob)
    envelope["state"]["event_state"][field] = value
    result = prepare_restore(
        pickle.dumps(envelope), entry, machine.code, entry.region.execution, "strict"
    )
    assert not result["decision"]["allow"]


def test_snapshot_cursor_and_prefix_must_agree(event_machine):
    machine, _, _ = event_machine
    entry = machine.code.entries["main"]
    state = initial(machine, "1" * 32)
    state["event_state"].update(next_sequence=1, committed_watermark=0)
    with pytest.raises(ValueError, match="prefix"):
        encode_snapshot(state, entry, machine.code, entry.region.execution)
    event = CommittedEvent(
        "1" * 32, 0, "2" * 64, "test", Event.capture("test", 7).to_dict()["payload"]
    )
    state["stats"]["committed_events"]["events"].append(event.to_dict())
    blob = encode_snapshot(state, entry, machine.code, entry.region.execution)
    assert prepare_restore(blob, entry, machine.code, entry.region.execution, "strict")["decision"][
        "allow"
    ]


def test_older_snapshot_behind_journal_rejects_before_emit(event_machine):
    machine, journal, _ = event_machine
    entry = machine.code.entries["main"]
    uid = "1" * 32
    state = initial(machine, uid)
    blob = encode_snapshot(state, entry, machine.code, entry.region.execution)
    journal.append(CommittedEvent(uid, 0, "2" * 64, "prior", None))
    machine.store.put(snapshot.snapshot_key(entry.region.semantic_hash, "job", digest({})), blob)
    result = machine.submit({}, key="job").result(5)
    assert not result.ok and result.failure["kind"] == "EventJournalIncompatible"
    assert journal.frontier(uid) == 0
    assert result.record is None


class BadSize:
    @property
    def nbytes(self):
        raise ValueError("restored size unavailable")


def test_async_restore_admission_error_settles_handle_and_releases_reserved_key(event_machine):
    machine, journal, _ = event_machine
    entry = machine.code.entries["main"]
    state = initial(machine, "1" * 32)
    state["values"]["opaque"] = BadSize()
    key = snapshot.snapshot_key(entry.region.semantic_hash, "job", digest({}))
    machine.store.put(key, encode_snapshot(state, entry, machine.code, entry.region.execution))
    result = machine.submit({}, key="job").result(5)
    assert not result.ok and result.failure["kind"] == "MachineError"
    assert "restored size unavailable" in result.failure["message"]
    assert not machine.dispatcher.jobs and "job" not in machine.dispatcher.keys
    assert machine.dispatcher.pool_bytes == 0 and machine.events.pending_restores == 0
    assert journal.frontier("1" * 32) == -1
    machine.store.delete(key)
    assert machine.submit({}, key="job").result(5).ok


def test_compatible_frontier_resume_keeps_full_prefix_and_next_sequence(event_machine):
    machine, journal, _ = event_machine
    entry = machine.code.entries["main"]
    uid = "1" * 32
    state = initial(machine, uid)
    event = CommittedEvent(uid, 0, "2" * 64, "prior", None)
    journal.append(event)
    state["event_state"].update(next_sequence=1, committed_watermark=0)
    state["stats"]["committed_events"]["events"].append(event.to_dict())
    blob = encode_snapshot(state, entry, machine.code, entry.region.execution)
    machine.store.put(snapshot.snapshot_key(entry.region.semantic_hash, "job", digest({})), blob)
    result = machine.submit({}, key="job").result(5)
    assert result.ok, result.failure
    assert result.stats["resumed"]
    events = result.stats["committed_events"]["events"]
    assert [row["sequence"] for row in events] == [0, 1]
    assert events[0] == event.to_dict()
    assert journal.frontier(uid) == 1
