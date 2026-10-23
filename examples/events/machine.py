"""Ordered semantic events and outcome replay; no engine streaming or external services."""

from pathlib import Path

from togeari import EventOutput, Inline, Machine
from togeari.conformance import conform
from togeari.io import Iterable, Memory, MemoryEventJournal, MemoryEventSink

HERE = Path(__file__).parent
INPUTS = [{"items": [1, 1, 2]}, {"items": []}]


def build():
    journal = MemoryEventJournal()  # Explicitly nondurable; use SQLiteEventJournal for restart.
    consumer = MemoryEventSink()
    machine = Machine(
        program=HERE / "events.kir",
        entry="main",
        bind={"kir.std.*": Inline()},
        event_output=EventOutput(consumer, journal, max_events=2, max_bytes=4096),
        record=1.0,
    )
    return machine, Iterable(INPUTS), Memory()


def main():
    machine, source, sink = build()
    journal = machine.event_output.journal
    consumer = machine.event_output.sink
    try:
        with machine:
            machine.run(source, sink)
            program = machine.code.entries["main"].region.job
        for record in sink.records:
            verdict = conform(program, record, INPUTS[int(record.key)])
            if not record.ok or not verdict.ok or verdict.event_status != "verified":
                raise RuntimeError(record.failure or verdict.reason)
        events = sorted(consumer.events, key=lambda event: (event.job_execution_id, event.sequence))
        if [event.sequence for event in events] != [0, 1, 2]:
            raise RuntimeError("semantic event order differs")
        if len({event.event_id for event in events}) != 3:
            raise RuntimeError("equal payloads reused event identity")
        if journal.pending():
            raise RuntimeError("delivery remains pending; terminal jobs alone do not imply ack")
        print(f"events: {len(events)} committed and acknowledged; equal payloads have distinct IDs")
        print(f"conformance: {len(sink.records)}/{len(INPUTS)} jobs match the reference")
    finally:
        journal.close()  # The application owns journal lifetime.


if __name__ == "__main__":
    main()
