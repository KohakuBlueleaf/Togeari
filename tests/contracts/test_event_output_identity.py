"""Semantic output compatibility is scoped to authentic emission regions."""

import pytest

from togeari import EventOutput, Inline, Machine
from togeari.core.program.execution import compatibility, current_execution
from togeari.io import MemoryEventJournal, MemoryEventSink


@pytest.fixture
def machines():
    made = []

    def make(output, source='("topic", 1)emit()', binding=None):
        machine = Machine(
            program=source,
            bind={} if binding is None else {"*": binding},
            event_output=output,
            core="python",
            start_units=False,
        )
        made.append(machine)
        return machine

    yield make
    for machine in made:
        machine.stop()


def manifest(machine):
    return current_execution(machine.code.regions[0], machine.code, machine)


def test_output_journal_limits_and_sink_revision_are_identity(machines):
    journal = MemoryEventJournal()
    first = machines(EventOutput(MemoryEventSink(), journal, sink_revision="sink/1"))
    base = manifest(first)
    assert base["completeness"]["complete"]
    equivalent = machines(
        EventOutput(
            MemoryEventSink(),
            journal,
            max_events=2,
            retry_s=1,
            close_timeout_s=0,
            sink_revision="sink/1",
        )
    )
    assert manifest(equivalent)["hash"] == base["hash"]
    for options in ({"max_bytes": 1000}, {"sink_revision": "sink/2"}):
        config = {"sink_revision": "sink/1", **options}
        changed = machines(EventOutput(MemoryEventSink(), journal, **config))
        assert not compatibility(base, manifest(changed), "warn")["allow"]
    other_journal = machines(
        EventOutput(MemoryEventSink(), MemoryEventJournal(), sink_revision="sink/1")
    )
    assert manifest(other_journal)["hash"] != base["hash"]


def test_unknown_sink_revision_visible_and_strict_refuses(machines):
    machine = machines(EventOutput(MemoryEventSink(), MemoryEventJournal()))
    current = manifest(machine)
    assert any(
        gap["path"] == "runtime_policy.event_output.sink_revision"
        for gap in current["completeness"]["unknown"]
    )
    assert compatibility(current, current, "warn")["allow"]
    assert not compatibility(current, current, "strict")["allow"]


def test_emit_bundle_attestation_can_cover_unknown_sink_revision(machines):
    machine = machines(
        EventOutput(MemoryEventSink(), MemoryEventJournal()),
        binding=Inline(binding_revision="emit-and-delivery/1"),
    )
    current = manifest(machine)
    assert current["completeness"]["complete"]
    assert compatibility(current, current, "strict")["allow"]
    assert current["identity"]["calls"][0]["binding_revision"] == "emit-and-delivery/1"


def test_nonemit_region_identity_unaffected_by_output_configuration(machines):
    source = "(1)identity(out)"
    plain = machines(None, source, Inline())
    configured = machines(EventOutput(MemoryEventSink(), MemoryEventJournal()), source, Inline())
    assert manifest(plain)["hash"] == manifest(configured)["hash"]
    assert "event_output" not in manifest(configured)["identity"]["runtime_policy"]


def test_descriptor_is_cached_no_hotpath_journal_io(machines):
    journal = MemoryEventJournal()
    output = EventOutput(MemoryEventSink(), journal, sink_revision="sink/1")
    machine = machines(output)
    initial = manifest(machine)
    journal.identity = lambda: (_ for _ in ()).throw(AssertionError("hotpath IO"))
    assert manifest(machine) is initial
