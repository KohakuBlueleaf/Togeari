"""Bounded imported records/collect/events/resources/checkpoint continuation fixture."""

import json
import pickle
import queue

import pytest
from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.conformance import conform
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.events import EventOutput
from togeari.core.contracts.results import Ok
from togeari.core.runtime.checkpoints import checkpoint_key
from togeari.io.events import MemoryEventJournal, MemoryEventSink
from togeari.policies import BudgetFormer
from togeari.stores import MemoryStore

LIB = NodeLibrary("stabilize")
LIB.record("Value", [("value", int)])


@LIB.node()
def work(value: int, stage: str) -> int:
    return value + 1


MODULE = """@use "stabilize" as h
@export mapped
@export controlled
@def (value: int)mapped(result: int):
    (value)h.Value.make(record)
    (record)h.Value.get.value(extracted)
    (extracted, "map")h.work(result)
@def (value: int, stage: str)controlled(result: int):
    (value, stage)h.work(result)
"""
MAIN = """@import "flow.kir" as flow
@def (xs: list[int])main(values: list[int], result: int):
    (xs)collect(x, `body`, y -> values)
    body:
        (x)flow.mapped(y)
    ("prefix", values)emit()
    (values)len(count)
    (count, "prefix")flow.controlled(boundary)
    (boundary, "suffix")flow.controlled(result)
    ("suffix", result)emit()
@def ()ping(result: int):
    (40, 2)add(result)
"""


class Controlled(EngineBase):
    kind = "batch"
    max_batch = 4
    protocol = "python"

    def __init__(self):
        super().__init__("stabilization")
        self.submissions = queue.Queue()
        self.calls = []

    def capacity(self):
        return 8

    def key(self, item):
        return item.inputs["stage"]

    def submit(self, items, done):
        ticket = len(self.calls) + 1
        self.calls.extend((item.inputs["stage"], item.inputs["value"]) for item in items)
        self.submissions.put((ticket, list(items), done))
        return ticket

    def cancel(self, ticket):
        pass


def complete(submission):
    ticket, items, done = submission
    done(ticket, [Ok(item.inputs["value"] + 1) for item in items])


def build(path, core, store, journal):
    engine = Controlled()
    lane = Lane(
        [engine], former=BudgetFormer({"tokens": 4}, lambda item: {"tokens": 1}, size=1, wait_ms=0)
    )
    machine = Machine(
        programs={path: ["main", "ping"]},
        entry="main",
        libraries=[LIB],
        core=core,
        bind={
            "stabilize.work": Use(lane, deterministic=True, binding_revision="work/1"),
            "stabilize.Value.*": Inline(),
            "kir.std.*": Inline(),
        },
        record=1.0,
        store=store,
        event_output=EventOutput(
            MemoryEventSink(), journal, max_events=2, max_bytes=8192, sink_revision="sink/1"
        ),
    )
    return machine, engine, lane


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("stale", [False, True], ids=["matching", "stale"])
def test_imported_collect_event_checkpoint_continuation(tmp_path, core, stale):
    (tmp_path / "flow.kir").write_text(MODULE, encoding="utf-8")
    path = tmp_path / "main.kir"
    path.write_text(MAIN, encoding="utf-8")
    store, journal = MemoryStore(), MemoryEventJournal()
    inputs = {"xs": [3, 1]}
    machine, engine, lane = build(path, core, store, journal)
    resumed_machine = None
    try:
        handle = machine.submit(inputs, key="original")
        first = engine.submissions.get(timeout=5)
        # Bound each batch to one, then force completion opposite to input order.
        second = engine.submissions.get(timeout=5)
        assert [item.inputs["value"] for item in first[1]] == [3]
        assert [item.inputs["value"] for item in second[1]] == [1]
        complete(second)
        complete(first)
        prefix = engine.submissions.get(timeout=5)
        assert [item.inputs["stage"] for item in prefix[1]] == ["prefix"]
        [resident] = machine.inspect("jobs").result(5)
        waits = machine.inspect("waits", job=resident["id"], limit=256).result(5)
        assert any(edge["reason"] == "input" for edge in waits["relations"])
        assert "flow.kir" in json.dumps(waits)
        assert not waits["truncated"]
        checkpoint = machine.command("checkpoint_job", id=resident["id"], timeout_s=5)
        [pending] = machine.inspect("checkpoints", job=resident["id"]).result(5)
        assert pending["status"] == "draining"
        assert machine.submit({}, entry="ping", key="independent").result(5).outputs == {
            "result": 42
        }
        complete(prefix)
        suffix = engine.submissions.get(timeout=5)
        assert [item.inputs["stage"] for item in suffix[1]] == ["suffix"]
        saved = checkpoint.result(5)
        assert saved["status"] == "persisted"
        artifact = pickle.loads(store.get(checkpoint_key(saved["checkpoint_id"])))
        state = pickle.loads(artifact["snapshot"])["state"]
        uid = state["job_execution_id"]
        assert state["event_state"]["committed_watermark"] == journal.frontier(uid) == 0
        assert state["event_state"]["next_sequence"] == 1
        prefix_events = state["stats"]["committed_events"]["events"]
        assert len(prefix_events) == 1
        if stale:
            complete(suffix)
            original = handle.result(5)
            assert original.ok and original.outputs == {"values": [4, 2], "result": 4}
            assert conform(machine.code.entries["main"].region.job, original, inputs).ok
            assert journal.frontier(uid) == 1
        machine.stop(drain=False, timeout=5)
        resumed_machine, resumed_engine, resumed_lane = build(path, core, store, journal)
        resumed = resumed_machine.resume(saved["checkpoint_id"], inputs, key="original")
        if stale:
            result = resumed.result(5)
            assert not result.ok and result.failure["kind"] == "EventJournalIncompatible"
            assert resumed_engine.calls == [] and journal.frontier(uid) == 1
        else:
            complete(resumed_engine.submissions.get(timeout=5))
            result = resumed.result(5)
            assert result.ok, result.failure
            assert result.outputs == {"values": [4, 2], "result": 4}
            assert result.stats["job_execution_id"] == uid
            assert resumed_engine.calls == [("suffix", 3)]
            assert engine.calls == [("map", 3), ("map", 1), ("prefix", 2), ("suffix", 3)]
            events = result.stats["committed_events"]["events"]
            assert events[:1] == prefix_events
            assert [event["sequence"] for event in events] == [0, 1]
            assert len({event["event_id"] for event in events}) == 2
            verdict = conform(resumed_machine.code.entries["main"].region.job, result, inputs)
            assert verdict.ok and verdict.event_status == "verified", verdict
        assert resumed_lane.resource_view()["total"] == {"tokens": 0.0}
        assert lane.resource_view()["total"] == {"tokens": 0.0}
    finally:
        machine.stop(drain=False, timeout=5)
        if resumed_machine is not None:
            resumed_machine.stop(drain=False, timeout=5)
        journal.close()
