"""Independent semantic-event integration: retirement, bounded output and durable recovery."""

import json
import pickle
import queue
import subprocess
import sys
import threading

import pytest
from kohakunode import NodeLibrary
from kohakunode.records import decode_portable

from togeari import ConstructionError, Inline, Machine, Use
from togeari.conformance import conform
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.events import EventConflict, EventOutput
from togeari.core.contracts.results import ItemError, Ok
from togeari.core.runtime.checkpoints import checkpoint_key
from togeari.io.events import SQLiteEventJournal
from togeari.stores import MemoryStore

LIB = NodeLibrary("committed_event_integration")
ORDERED = []


@LIB.node(ordered=True)
def ordered_note(value: int) -> int:
    ORDERED.append(value)
    return value


@LIB.node(mutates=["values"], outputs=["values"])
def push(values: list, value: int) -> list:
    values.append(value)
    return values


@LIB.node()
def work(value: int) -> int:
    if value < 0:
        raise ValueError("negative work")
    return value * 10


class Journal:
    """Thread-safe explicit nondurable journal with observable append/ack handshakes."""

    def __init__(self, *, held=False):
        self.events = []
        self.acked = set()
        self.lock = threading.Lock()
        self.append_entered = threading.Event()
        self.append_release = threading.Event()
        if not held:
            self.append_release.set()
        self.append_threads = []
        self.acknowledged = queue.Queue()

    def identity(self):
        return {"schema": "test.event_journal", "version": 1, "id": "integration", "durable": False}

    def append(self, event):
        self.append_threads.append(threading.get_ident())
        self.append_entered.set()
        assert self.append_release.wait(5), "test did not release journal append"
        with self.lock:
            if any(old.event_id == event.event_id for old in self.events):
                return
            assert event.sequence == 1 + max(
                (
                    old.sequence
                    for old in self.events
                    if old.job_execution_id == event.job_execution_id
                ),
                default=-1,
            )
            self.events.append(event)

    def pending(self, limit=64, max_bytes=None):
        with self.lock:
            out, size = [], 0
            for event in self.events:
                if event.event_id in self.acked:
                    continue
                if len(out) >= limit or (
                    max_bytes is not None and size + event.size_bytes > max_bytes
                ):
                    break
                out.append(event)
                size += event.size_bytes
            return out

    def ack(self, event_id):
        with self.lock:
            self.acked.add(event_id)
        self.acknowledged.put(event_id)

    def frontier(self, job_execution_id):
        with self.lock:
            return max(
                (
                    event.sequence
                    for event in self.events
                    if event.job_execution_id == job_execution_id
                ),
                default=-1,
            )

    def close(self):
        return None


class Sink:
    def __init__(self, *, held=False, fail_first=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        if not held:
            self.release.set()
        self.delivered = queue.Queue()
        self.attempts = []
        self.fail_first = fail_first

    def write_event(self, event):
        self.attempts.append(event.event_id)
        self.entered.set()
        assert self.release.wait(5), "test did not release sink"
        if self.fail_first:
            self.fail_first = False
            raise OSError("consumer temporarily unavailable")
        self.delivered.put(event)

    def close(self):
        return None


class Controlled(EngineBase):
    def __init__(self):
        super().__init__("controlled-events")
        self.submissions = queue.Queue()
        self.sequence = 0

    def capacity(self):
        return 8

    def submit(self, item, done):
        self.sequence += 1
        self.submissions.put((self.sequence, item.inputs["value"], done))
        return self.sequence


def make(core, source, *, journal=None, sink=None, max_events=8, max_bytes=8192, bind=None):
    journal = Journal() if journal is None else journal
    sink = Sink() if sink is None else sink
    machine = Machine(
        program=source,
        entry="main",
        libraries=[LIB],
        core=core,
        record=1.0,
        bind={"kir.std.*": Inline(), "ordered_note": Inline(), **(bind or {})},
        event_output=EventOutput(
            sink, journal, max_events=max_events, max_bytes=max_bytes, retry_s=0.001
        ),
        events="counters",
    )
    return machine, journal, sink


def stop(machine, journal, sink):
    journal.append_release.set()
    sink.release.set()
    machine.stop(drain=False, timeout=5)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_emit_requires_explicit_output_and_real_emit_alias_is_recognized(core):
    with pytest.raises(ConstructionError, match="event_output"):
        Machine(program='("topic", 1)emit()\n', core=core)
    source = """@use "kir.std" as standard
@def ()main(result):
    ("topic", 1)standard.emit()
    result = 7
"""
    machine, journal, sink = make(core, source)
    try:
        record = machine.submit({}, key="alias").result(5)
        event = sink.delivered.get(timeout=5)
        assert record.ok and record.outputs == {"result": 7}
        assert event.topic == "topic" and decode_portable(event.encoded_payload) == 1
        assert event.sequence == 0 and event.job_execution_id == record.stats["job_execution_id"]
        assert conform(machine.code.entries["main"].region.job, record, {}).ok
    finally:
        stop(machine, journal, sink)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_empty_iteration_emit_region_has_verified_empty_sidecar(core):
    source = """@def ()main(result):
    result = 7
    ([])each(value, `body`)
    body:
        ("never", value)emit()
"""
    machine, journal, sink = make(core, source)
    try:
        record = machine.submit({}, key="verified-empty").result(5)
        assert record.ok and record.outputs == {"result": 7}
        assert record.stats["committed_events"] == {"version": 1, "events": []}
        assert journal.events == [] and sink.delivered.empty()
        verdict = conform(machine.code.entries["main"].region.job, record, {})
        assert verdict.ok and verdict.event_status == "verified"
    finally:
        stop(machine, journal, sink)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_append_ack_is_retirement_barrier_before_younger_ordered_effect(core):
    source = """@def ()main(result):
    ("before", [1, 2])emit()
    (7)ordered_note(result)
"""
    ORDERED.clear()
    machine, journal, sink = make(core, source, journal=Journal(held=True))
    try:
        handle = machine.submit({}, key="append-gate")
        assert journal.append_entered.wait(5)
        machine.inspect("jobs").result(5)
        assert ORDERED == [] and not handle.done() and sink.delivered.empty()
        assert journal.append_threads[0] != threading.get_ident()
        journal.append_release.set()
        record = handle.result(5)
        event = sink.delivered.get(timeout=5)
        assert record.ok and ORDERED == [7]
        assert decode_portable(event.encoded_payload) == [1, 2]
        assert conform(machine.code.entries["main"].region.job, record, {}).ok
    finally:
        stop(machine, journal, sink)


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("bound", ["count", "bytes"])
def test_slow_sink_bound_blocks_second_emit_but_other_job_runs(core, bound):
    source = """@def ()main(result):
    ("same", 1)emit()
    ("same", 1)emit()
    (9)ordered_note(result)
@def ()fast(result):
    result = 42
"""
    if bound == "bytes":
        source = source.replace('("same", 1)', f'("same", {json.dumps("x" * 2200)})')
    journal, sink = Journal(), Sink(held=True)
    ORDERED.clear()
    machine = Machine(
        programs={source: ["main", "fast"]},
        entry="main",
        libraries=[LIB],
        core=core,
        bind={"kir.std.*": Inline(), "ordered_note": Inline()},
        record=1.0,
        events="counters",
        event_output=EventOutput(
            sink,
            journal,
            max_events=1 if bound == "count" else 8,
            max_bytes=8192 if bound == "count" else 4096,
            retry_s=0.001,
        ),
    )
    try:
        handle = machine.submit({}, key="blocked")
        assert sink.entered.wait(5)
        fast = machine.submit({}, key="fast", entry="fast").result(5)
        assert fast.ok and fast.outputs == {"result": 42}
        assert ORDERED == [] and not handle.done()
        assert journal.frontier(journal.events[0].job_execution_id) == 0
        if bound == "bytes":
            assert journal.events[0].size_bytes <= 4096 < 2 * journal.events[0].size_bytes
        sink.release.set()
        record = handle.result(5)
        first, second = sink.delivered.get(timeout=5), sink.delivered.get(timeout=5)
        assert record.ok and ORDERED == [9]
        assert (first.sequence, second.sequence) == (0, 1)
        assert first.event_id != second.event_id
        assert first.invocation_id != second.invocation_id
        assert first.encoded_payload == second.encoded_payload
        assert conform(machine.code.entries["main"].region.job, record, {}).ok
    finally:
        stop(machine, journal, sink)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_older_fault_squashes_younger_emit_but_keeps_committed_prefix(core):
    source = """@def ()main(result):
    ("prefix", 0)emit()
    (-1)work(result)
    ("younger", 1)emit()
"""
    engine = Controlled()
    machine, journal, sink = make(core, source, bind={"work": Use(engine)})
    try:
        handle = machine.submit({}, key="fault")
        first = sink.delivered.get(timeout=5)
        ticket, value, done = engine.submissions.get(timeout=5)
        assert value == -1
        done(ticket, [ItemError(ValueError("negative work"))])
        record = handle.result(5)
        assert not record.ok and record.failure["kind"] == "ValueError"
        assert first.topic == "prefix" and len(journal.events) == 1
        assert sink.delivered.empty()
        assert conform(machine.code.entries["main"].region.job, record, {}).ok
    finally:
        stop(machine, journal, sink)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_reversed_engine_completions_emit_in_input_order(core):
    source = """@def ()main(result):
    result = 0
    ([1, 2, 3])each(value, `body`)
    body:
        (value)work(result)
        ("value", result)emit()
"""
    engine = Controlled()
    machine, journal, sink = make(core, source, bind={"work": Use(engine)})
    try:
        handle = machine.submit({}, key="reordered")
        pending = [engine.submissions.get(timeout=5) for _ in range(3)]
        for ticket, value, done in reversed(pending):
            done(ticket, [Ok(value * 10, True)])
        record = handle.result(5)
        events = [sink.delivered.get(timeout=5) for _ in range(3)]
        assert record.ok
        assert [event.sequence for event in events] == [0, 1, 2]
        assert [decode_portable(event.encoded_payload) for event in events] == [10, 20, 30]
        assert conform(machine.code.entries["main"].region.job, record, {}).ok
    finally:
        stop(machine, journal, sink)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_sink_retry_uses_one_semantic_event_and_same_delivery_id(core):
    source = """@def ()main(result):
    ("retry", {"value": 1})emit()
    result = 5
"""
    machine, journal, sink = make(core, source, sink=Sink(fail_first=True))
    try:
        record = machine.submit({}, key="delivery-retry").result(5)
        event = sink.delivered.get(timeout=5)
        assert record.ok and journal.acknowledged.get(timeout=5) == event.event_id
        assert len(journal.events) == 1
        assert sink.attempts == [event.event_id, event.event_id]
        assert conform(machine.code.entries["main"].region.job, record, {}).ok
    finally:
        stop(machine, journal, sink)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_committed_payload_is_snapshot_despite_later_declared_host_mutation(core):
    source = """@def ()main(values):
    values = [1]
    ("captured", values)emit()
    (values, 2)push(values)
"""
    machine, journal, sink = make(core, source, sink=Sink(held=True), bind={"push": Inline()})
    try:
        handle = machine.submit({}, key="snapshot-payload")
        assert sink.entered.wait(5)
        record = handle.result(5)
        assert record.ok and record.outputs == {"values": [1, 2]}
        assert decode_portable(journal.events[0].encoded_payload) == [1]
        sink.release.set()
        event = sink.delivered.get(timeout=5)
        assert decode_portable(event.encoded_payload) == [1]
        assert conform(machine.code.entries["main"].region.job, record, {}).ok
    finally:
        stop(machine, journal, sink)


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize(
    "payload,expected", [("x" * 8192, "EventTooLarge"), (object(), "TypeError")]
)
def test_invalid_or_oversized_emit_is_catchable_and_never_appends(core, payload, expected):
    source = """@def (payload)main(error):
    error = None
    @try:
        ("invalid", payload)emit()
    @except caught:
        error = caught
"""
    machine, journal, sink = make(core, source, max_bytes=1024)
    try:
        record = machine.submit({"payload": payload}, key="rejected").result(5)
        assert record.ok and record.outputs["error"]["kind"] == expected
        assert journal.events == [] and sink.delivered.empty()
        assert len(record.record) == 1
        assert conform(machine.code.entries["main"].region.job, record, {"payload": payload}).ok
    finally:
        stop(machine, journal, sink)


class RejectingJournal(Journal):
    """Definitive append rejection or startup recovery failure, never transient retry."""

    def __init__(self, stage):
        super().__init__()
        self.stage = stage

    def append(self, event):
        if event.topic == "rejected":
            raise EventConflict("fixture append conflict")
        super().append(event)

    def pending(self, limit=64, max_bytes=None):
        if self.stage == "recovery":
            raise OSError("fixture recovery unavailable")
        return super().pending(limit, max_bytes)


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("stage", ["append", "recovery"])
def test_failed_event_commit_is_terminal_not_catchable(core, stage):
    prefix = '    ("prefix", 1)emit()\n' if stage == "append" else ""
    source = "@def ()main(result):\n" + prefix + """    result = 0
    @try:
        ("rejected", 2)emit()
        (11)ordered_note(result)
    @except caught:
        (22)ordered_note(result)
    (33)ordered_note(result)
"""
    ORDERED.clear()
    machine, journal, sink = make(core, source, journal=RejectingJournal(stage))
    try:
        record = machine.submit({}, key="commit-rejected").result(5)
        assert not record.ok
        assert record.failure["kind"] == "KirRuntimeError"
        cause = "fixture append conflict" if stage == "append" else "fixture recovery unavailable"
        assert cause in record.failure["message"]
        assert record.failure["node"].endswith("emit")
        assert ORDERED == []
        committed = record.stats["committed_events"]["events"]
        assert [event["topic"] for event in committed] == (["prefix"] if prefix else [])
        assert [event.topic for event in journal.events] == (["prefix"] if prefix else [])
        if prefix:
            journal.acknowledged.get(timeout=5)
        status = machine.inspect("event_output").result(5)["status"]
        assert status["pending_events"] == 0
        assert status["pending_bytes"] == 0
    finally:
        stop(machine, journal, sink)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_cancel_during_append_resolves_commit_instead_of_silently_discarding(core):
    source = """@def ()main(result):
    ("accepted", 1)emit()
    (10)ordered_note(result)
"""
    ORDERED.clear()
    machine, journal, sink = make(core, source, journal=Journal(held=True))
    try:
        handle = machine.submit({}, key="cancel-append")
        assert journal.append_entered.wait(5)
        [resident] = machine.inspect("jobs").result(5)
        assert machine.command("cancel_job", id=resident["id"]).result(5)
        assert not handle.done()
        journal.append_release.set()
        record = handle.result(5)
        assert len(journal.events) == 1
        assert journal.events[0].topic == "accepted"
        assert ORDERED == []
        assert not record.ok and record.failure["kind"] == "Cancelled"
    finally:
        stop(machine, journal, sink)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_fresh_process_append_before_ack_recovers_durable_event_without_reexecution(tmp_path, core):
    path = tmp_path / "events.sqlite"
    child = """import json, os, sys
from kohakunode.records import encode_portable
from togeari.core.contracts.events import CommittedEvent
from togeari.io.events import SQLiteEventJournal
journal = SQLiteEventJournal(sys.argv[1])
event = CommittedEvent("a" * 32, 0, "b" * 64, "crash-prefix", encode_portable({"value": 7}))
journal.append(event)
print(json.dumps({"id": event.event_id, "identity": journal.identity()}), flush=True)
os._exit(0)
"""
    process = subprocess.run(
        [sys.executable, "-c", child, str(path)], capture_output=True, text=True, timeout=15
    )
    assert process.returncode == 0, process.stderr
    saved = json.loads(process.stdout)
    journal, sink = SQLiteEventJournal(path), Sink()
    machine = Machine(
        program="@def ()main(result):\n    result = 42\n",
        entry="main",
        core=core,
        event_output=EventOutput(sink, journal, retry_s=0.001),
    )
    try:
        record = machine.submit({}, key="unrelated").result(5)
        event = sink.delivered.get(timeout=5)
        assert record.ok and record.outputs == {"result": 42}
        assert journal.identity() == saved["identity"]
        assert event.event_id == saved["id"] and event.sequence == 0
        assert decode_portable(event.encoded_payload) == {"value": 7}
        assert journal.frontier("a" * 32) == 0
    finally:
        machine.stop(drain=False, timeout=5)
        journal.close()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_checkpoint_behind_committed_journal_frontier_rejects_without_reexecution(core):
    source = """@def ()main(result):
    ("prefix", 0)emit()
    (1)work(result)
    ("suffix", result)emit()
"""
    journal, sink, store, engine = Journal(), Sink(), MemoryStore(), Controlled()
    machine = Machine(
        program=source,
        entry="main",
        libraries=[LIB],
        core=core,
        bind={"work": Use(engine, binding_revision="work/1"), "kir.std.*": Inline()},
        record=1.0,
        store=store,
        event_output=EventOutput(sink, journal, sink_revision="sink/1"),
    )
    try:
        handle = machine.submit({}, key="checkpoint-event")
        assert sink.delivered.get(timeout=5).topic == "prefix"
        ticket, value, done = engine.submissions.get(timeout=5)
        [resident] = machine.inspect("jobs").result(5)
        reply = machine.command("checkpoint_job", id=resident["id"], timeout_s=5)
        [pending] = machine.inspect("checkpoints", job=resident["id"]).result(5)
        assert pending["status"] == "draining"
        done(ticket, [Ok(value * 10, True)])
        saved = reply.result(5)
        assert saved["status"] == "persisted"
        original = handle.result(5)
        assert original.ok
        assert sink.delivered.get(timeout=5).topic == "suffix"
        artifact = pickle.loads(store.get(checkpoint_key(saved["checkpoint_id"])))
        state = pickle.loads(artifact["snapshot"])["state"]
        assert state["event_state"]["committed_watermark"] == 0
        assert state["event_state"]["next_sequence"] == 1
        assert journal.frontier(original.stats["job_execution_id"]) == 1
        resumed = machine.resume(saved["checkpoint_id"], {}, key="checkpoint-event").result(5)
        assert not resumed.ok and resumed.failure["kind"] == "EventJournalIncompatible"
        assert engine.submissions.empty()
        assert len(journal.events) == 2
    finally:
        stop(machine, journal, sink)


@pytest.mark.serial
@pytest.mark.parametrize("core", ["python", "rust"])
def test_bounded_shutdown_reports_unknown_append_without_claiming_absence(core):
    journal, sink = Journal(held=True), Sink()
    machine = Machine(
        program='@def ()main():\n    ("held", 1)emit()\n',
        entry="main",
        core=core,
        event_output=EventOutput(sink, journal, close_timeout_s=0.05),
    )
    stopped, errors = threading.Event(), []

    def shutdown():
        try:
            machine.stop(drain=False, timeout=0.2)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            stopped.set()

    worker = None
    try:
        machine.submit({}, key="unknown")
        assert journal.append_entered.wait(5)
        worker = threading.Thread(target=shutdown)
        worker.start()
        assert stopped.wait(2), "shutdown waited indefinitely for journal append"
        assert errors == []
        assert machine.event_shutdown["status"] == "pending"
        assert len(machine.event_shutdown["unknown_appends"]) == 1
        assert not journal.append_release.is_set()
    finally:
        journal.append_release.set()
        sink.release.set()
        if worker is not None:
            worker.join(5)
        machine.stop(drain=False, timeout=5)
