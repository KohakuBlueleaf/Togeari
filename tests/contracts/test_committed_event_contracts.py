"""Immutable committed transport and explicit output configuration."""

import pickle
from dataclasses import FrozenInstanceError

import pytest

from togeari import CommittedEvent, EventOutput
from togeari.core.contracts.events import event_id
from togeari.io import MemoryEventJournal, MemoryEventSink

JOB = "a" * 32
INV = "b" * 64


def test_event_identity_and_payload_are_detached_immutable():
    payload = {"list": [1, {"x": 2}]}
    origin = {"module": "example", "line": 1}
    event = CommittedEvent(JOB, 0, INV, "topic", payload, origin)
    payload["list"].clear()
    origin["line"] = 9
    assert event.encoded_payload == {"list": [1, {"x": 2}]}
    assert event.origin["line"] == 1
    event.to_dict()["encoded_payload"]["list"].clear()
    assert event.encoded_payload["list"]
    assert event.event_id == event_id(INV)
    assert event.size_bytes == len(event.encoded_bytes())
    assert CommittedEvent.from_dict(event.to_dict()) == event
    with pytest.raises(FrozenInstanceError):
        event.sequence = 9
    with pytest.raises(TypeError):
        event.__init__(JOB, 1, "c" * 64, "other", None)
    assert event.sequence == 0


def test_huge_integer_lossless_bytes_pickle_and_state_guards():
    huge = 10**5000
    event = CommittedEvent(JOB, 0, INV, "huge", {"value": huge})
    assert CommittedEvent.from_bytes(event.encoded_bytes()).encoded_payload == {"value": huge}
    assert pickle.loads(pickle.dumps(event)) == event
    with pytest.raises(TypeError):
        event.__setstate__(["replaced"])
    output = EventOutput(MemoryEventSink(), MemoryEventJournal())
    with pytest.raises(TypeError):
        output.__setstate__(["replaced"])


def test_equal_payload_distinct_invocations_and_origin_not_in_event_id():
    first = CommittedEvent(JOB, 0, INV, "topic", None)
    other = CommittedEvent(JOB, 1, "c" * 64, "topic", None)
    relocated = CommittedEvent(JOB, 0, INV, "topic", None, {"line": 99})
    assert first.event_id != other.event_id
    assert first.event_id == relocated.event_id


@pytest.mark.parametrize("payload", [object(), float("inf"), {1: "not-string"}, (1, 2)])
def test_transport_rejects_nonportable_values(payload):
    with pytest.raises((TypeError, ValueError)):
        CommittedEvent(JOB, 0, INV, "topic", payload)


def test_cyclic_transport_and_tampered_envelope_rejected():
    payload = []
    payload.append(payload)
    with pytest.raises(ValueError, match="cyclic"):
        CommittedEvent(JOB, 0, INV, "topic", payload)
    event = CommittedEvent(JOB, 0, INV, "topic", None)
    tampered = event.to_dict()
    tampered["event_id"] = "0" * 64
    with pytest.raises(ValueError, match="ID differs"):
        CommittedEvent.from_dict(tampered)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_events": 0},
        {"max_bytes": True},
        {"retry_s": 0},
        {"retry_s": float("nan")},
        {"close_timeout_s": -1},
        {"sink_revision": ""},
    ],
)
def test_output_invalid_config(kwargs):
    with pytest.raises((TypeError, ValueError)):
        EventOutput(MemoryEventSink(), MemoryEventJournal(), **kwargs)


def test_output_requires_both_and_caches_identity_once():
    with pytest.raises(TypeError):
        EventOutput(MemoryEventSink(), None)
    journal = MemoryEventJournal()
    output = EventOutput(MemoryEventSink(), journal)
    original = output.descriptor()
    journal.identity = lambda: (_ for _ in ()).throw(AssertionError("no repeated I/O"))
    assert output.descriptor() == original
    output.descriptor()["journal"]["journal_id"] = "changed"
    assert output.descriptor() == original
    with pytest.raises(FrozenInstanceError):
        output.max_bytes = 1
    with pytest.raises(TypeError):
        output.__init__(None, None, max_bytes=1)
    assert output.max_bytes == 8 << 20
