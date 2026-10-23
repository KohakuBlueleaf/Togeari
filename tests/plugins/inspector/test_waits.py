"""Bounded metadata-only wait explanations; synthetic states and fake clocks."""

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from togeari import Machine, Use
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.resources import Estimate
from togeari.core.contracts.results import Ok
from togeari.core.contracts.workitem import WorkItem
from togeari.core.lanes.lane import Lane
from togeari.core.lanes.resources import Resources
from togeari.core.observe.waits import _python_state, waits
from togeari.core.runtime.state import (
    Frame,
    Inflight,
    InfState,
    Job,
    Join,
    NameMap,
    Slot,
    SlotState,
    Strand,
    StrandState,
)


class HeldEngine(EngineBase):
    def __init__(self):
        super().__init__("held")
        self.submitted = threading.Event()
        self.pending = []

    def capacity(self):
        return 1

    def submit(self, item, done):
        self.pending.append(done)
        self.submitted.set()
        return len(self.pending)

    def release(self):
        callbacks, self.pending = self.pending, []
        for callback in callbacks:
            callback(1, [Ok(-1)])


@pytest.mark.parametrize("core", ["python", "rust"])
def test_actual_dispatcher_query_input_producer_and_detached_snapshot(core):
    if core == "rust":
        pytest.importorskip("togeari._rs")
    engine = HeldEngine()
    m = Machine(program="(1)neg(a)\n(a)neg(b)\n", bind={"neg": Use(engine)}, core=core)
    try:
        m.submit({})
        assert engine.submitted.wait(5)
        [resident] = m.inspect("jobs").result(5)
        with pytest.raises(KeyError, match="resident job"):
            m.inspect("waits", job=resident["id"] + 1000000).result(5)
        out = m.inspect("waits", job=resident["id"], limit=256).result(5)
        assert "native_wait_state" not in out["unsupported"]
        entities = refs(out)
        [edge] = [e for e in out["relations"] if e["reason"] == "input" and "port" in e["detail"]]
        assert entities[edge["from"]]["kind"] == "inflight"
        assert entities[edge["to"]]["kind"] == "slot"
        assert any(
            e["from"] == edge["to"] and e["detail"].get("subreason") == "producer"
            for e in out["relations"]
        )
        out["entities"].clear()
        again = m.inspect("waits", job=resident["id"], limit=256).result(5)
        assert again["entities"] and again["run_id"] == out["run_id"]
        assert again["snapshot_id"] > out["snapshot_id"]
    finally:
        engine.release()
        m.stop(drain=False)


class MachineView:
    pass


class ForbiddenValue:
    def __repr__(self):
        raise AssertionError("wait inspection accessed a value")


def machine():
    m = MachineView()
    m.core = "python"
    m.knobs = {"window": 8, "retire_buffer": 8, "mutates": "wait"}
    m.code = SimpleNamespace(mem=[])
    m.dispatcher = SimpleNamespace(jobs={}, clock=lambda: 10.0)
    m.lanes = []
    return m


def instruction(m, *, ordered=False, mutates=(), operand=None, lane=None):
    origin = {
        "module": "lib.kir",
        "node_id": "same-source",
        "line": 7,
        "definition": "work",
        "calls": [{"module": "main.kir", "line": 3}],
        "generated": None,
    }
    ins = SimpleNamespace(
        addr=len(m.code.mem),
        node="work",
        op="call",
        ordered=ordered,
        mutates=mutates,
        operand=operand,
        binding=SimpleNamespace(lane=lane),
        origin_dict=lambda: copy.deepcopy(origin),
    )
    m.code.mem.append(ins)
    return ins


def job(m):
    value = Job("source-key", SimpleNamespace(name="main"), {})
    m.dispatcher.jobs[value.id] = value
    return value


def inflight(j, ins, key, state=InfState.WAITING):
    value = Inflight(j, ins, key)
    value.state = state
    j.add(value)
    if state in (InfState.WAITING, InfState.READY, InfState.ISSUED):
        j.unfinished += 1
    return value


def refs(snapshot):
    return {row["id"]: row for row in snapshot["entities"]}


def test_effective_forwarded_mutation_slot_and_producer_preserved():
    m = machine()
    j = job(m)
    ins = instruction(m)
    before = Slot(ForbiddenValue(), SlotState.READY)
    version = Slot()
    later = Slot()
    before.muts = [((1,), version), ((4,), later)]
    alias = Slot(phi=True)
    alias.forward = before
    producer = inflight(j, ins, (1,), InfState.ISSUED)
    producer.versions = {"xs": (before, version)}
    consumer = inflight(j, ins, (2,))
    consumer.inputs = [("xs", alias)]
    out = waits(m, job=j.id)
    entities = refs(out)
    [read] = [edge for edge in out["relations"] if edge["detail"].get("port") == "xs"]
    assert read["from"].endswith(f"inf:{j.id}:{consumer.id}")
    assert read["to"].endswith(f"slot:{j.id}:{version.id}")
    assert any(
        edge["from"] == read["to"] and edge["to"].endswith(f"inf:{j.id}:{producer.id}")
        for edge in out["relations"]
    )
    assert entities[read["from"]]["origin"] == ins.origin_dict()
    assert json.loads(json.dumps(out)) == out


def test_parked_control_and_phi_owners_are_strands_not_fake_calls():
    m = machine()
    j = job(m)
    cond = Slot()
    phi = Slot(phi=True)
    ins = instruction(m, operand=("n", "cond"))
    strand = Strand(j, ins.addr, (), 1, (), NameMap({"cond": cond}))
    strand.state = StrandState.PARKED
    strand.frames = (Frame(1, ((), 1), Join({"x": phi}, None, False), 0),)
    j.add_strand(strand)
    user = inflight(j, ins, (2,))
    user.inputs = [("x", phi)]
    out = waits(m)
    entity = refs(out)
    assert any(
        edge["reason"] == "control"
        and entity[edge["from"]]["kind"] == "strand"
        and edge["to"].endswith(f"slot:{j.id}:{cond.id}")
        for edge in out["relations"]
    )
    assert any(
        edge["reason"] == "control"
        and edge["from"].endswith(f"slot:{j.id}:{phi.id}")
        and edge["to"].endswith(f"strand:{j.id}:{strand.id}")
        for edge in out["relations"]
    )
    assert sum(row["kind"] == "inflight" for row in out["entities"]) == 1
    assert (
        next(row for row in out["entities"] if row["kind"] == "strand")["phase"]
        == "not_materialized"
    )


def test_effect_and_window_caps_use_read_only_evidence_and_progress_exception():
    m = machine()
    m.knobs["window"] = 1
    j = job(m)
    ins = instruction(m)
    older = inflight(j, ins, (0,))
    effect = inflight(j, instruction(m, ordered=True), (3,), InfState.READY)
    oldest = Strand(j, ins.addr, (), 1, (), NameMap())
    younger = Strand(j, ins.addr, (), 4, (), NameMap())
    j.add_strand(oldest)
    j.add_strand(younger)
    parked = Strand(j, ins.addr, (), 5, (), NameMap())
    parked.state = StrandState.PARKED
    j.add_strand(parked)
    heaps = (list(j.sheap), list(j.wheap))
    out = waits(m)
    assert heaps == (j.sheap, j.wheap)
    effects = [e for e in out["relations"] if e["reason"] == "effect"]
    assert any(
        e["from"].endswith(f"inf:{j.id}:{effect.id}") and e["to"].endswith(f"inf:{j.id}:{older.id}")
        for e in effects
    )
    windows = [
        e
        for e in out["relations"]
        if e["reason"] == "window" and e["detail"].get("subreason") != "occupant"
    ]
    assert len(windows) == 1 and windows[0]["from"].endswith(f"strand:{j.id}:{younger.id}")


def test_checkpoint_is_not_inferred_from_pause_and_terminal_fault_not_wait():
    m = machine()
    j = job(m)
    j.paused = True
    ins = instruction(m)
    ready = inflight(j, ins, (1,), InfState.READY)
    terminal = inflight(j, ins, (2,), InfState.DONE)
    terminal.fault = {"kind": "BudgetBlocked"}
    before = waits(m)
    assert not any(e["reason"] == "checkpoint" for e in before["relations"])
    assert not any(e["from"].endswith(f"inf:{j.id}:{terminal.id}") for e in before["relations"])
    j.cut = SimpleNamespace(request_id="request", phase="draining", frontier=(0,), held=[ready])
    after = waits(m)
    assert any(
        e["reason"] == "checkpoint" and e["from"].endswith(f"inf:{j.id}:{ready.id}")
        for e in after["relations"]
    )


def lane_for(item, *, phase):
    def forbidden(*args):
        raise AssertionError("query called an engine/policy callback")

    entry = SimpleNamespace(bucket=SimpleNamespace(deadline=20.0))
    queue = SimpleNamespace(index={id(item): entry})
    lane = Lane([], name="lane")
    lane.kind = "batch"
    lane._q = queue
    lane.state = {1: SimpleNamespace(state="up", queue=queue)}
    lane.capacity = forbidden
    lane._former = SimpleNamespace(take=forbidden, deadline=forbidden)
    if phase == "lane_capacity":
        lane.max_inflight = lane.inflight = 1
    elif phase == "retry_backoff":
        lane._held_ids[id(item)] = item
        lane._held = [(15.0, 0, item)]
    elif phase == "running":
        lane.subs[1] = SimpleNamespace(items=[item])
    return lane


@pytest.mark.parametrize("phase", ["lane_capacity", "batch_wait", "retry_backoff", "running"])
def test_cached_lane_reasons_fake_clock_and_stable_edges(phase):
    m = machine()
    j = job(m)
    item = object()
    lane = lane_for(item, phase=phase)
    inf = inflight(j, instruction(m, lane=lane), (0,), InfState.ISSUED)
    inf.item = item
    one = waits(m)
    m.dispatcher.clock = lambda: 11.0
    two = waits(m)
    assert one["run_id"] == two["run_id"] and two["snapshot_id"] == one["snapshot_id"] + 1
    if phase == "running":
        assert not one["relations"]
    else:
        [edge] = [e for e in one["relations"] if e["reason"] == phase]
        [later] = [e for e in two["relations"] if e["reason"] == phase]
        assert edge["id"] == later["id"]
        if phase != "lane_capacity":
            assert later["detail"]["remaining_s"] == edge["detail"]["remaining_s"] - 1


@pytest.mark.parametrize("marker", [None, {"reason": "budget_prefix"}])
def test_resource_wait_requires_actual_cached_prefix_marker_not_cost(marker):
    m = machine()
    j = job(m)
    item = WorkItem("work", {}, None)
    lane = lane_for(item, phase="batch_wait")
    resources = Resources(SimpleNamespace(budgets={"bytes": 1024}))
    resources.move(item, "queued", Estimate(object(), (("bytes", 1000),)))
    lane._resources = resources
    item._resource_wait = marker
    inf = inflight(j, instruction(m, lane=lane), (0,), InfState.ISSUED)
    inf.item = item
    out = waits(m)
    reasons = {edge["reason"] for edge in out["relations"]}
    assert ("resource_budget" in reasons) is (marker is not None)
    [edge] = [e for e in out["relations"] if e["reason"] in ("batch_wait", "resource_budget")]
    assert edge["detail"]["resources"]["costs"] == {"bytes": 1000.0}
    resources.pending.append((item, object()))
    terminal = waits(m)
    assert not any(e["reason"] == "resource_budget" for e in terminal["relations"])


def test_core_lane_scans_share_one_global_budget(monkeypatch):
    m = machine()
    j = job(m)
    seen = []

    def core_helper(self, item, now, limit=1024):
        seen.append(limit)
        return {"phase": "unknown", "scanned": limit, "truncated": True}

    monkeypatch.setattr(Lane, "_wait_item", core_helper, raising=False)
    lane = Lane([], name="bounded")
    ins = instruction(m, lane=lane)
    for index in range(8):
        inf = inflight(j, ins, (index,), InfState.ISSUED)
        inf.item = object()
    out = waits(m, limit=64)
    assert len(seen) == 1
    assert sum(seen) <= 4 * 64
    assert out["truncated"]


def test_custom_lane_inspection_callback_is_never_called():
    m = machine()
    j = job(m)

    class CustomLane(Lane):
        def _wait_item(self, item, now):
            raise AssertionError("user callback invoked")

    lane = CustomLane([], name="custom")
    inf = inflight(j, instruction(m, lane=lane), (0,), InfState.ISSUED)
    inf.item = object()
    out = waits(m)
    assert "custom_lane_wait_state" in out["unsupported"]


@pytest.mark.parametrize("limit", [1, 2, 5, 10, 30])
def test_total_output_bound_no_dangling_relations_or_payload(limit):
    m = machine()
    j = job(m)
    inf = inflight(j, instruction(m), (0,))
    inf.inputs = [(str(i), Slot(ForbiddenValue())) for i in range(100)]
    out = waits(m, limit=limit)
    assert len(out["entities"]) + len(out["relations"]) <= limit
    assert out["truncated"]
    assert any(row["kind"] == "summary" for row in out["entities"])
    ids = set(refs(out))
    assert all(edge["from"] in ids and edge["to"] in ids for edge in out["relations"])
    json.dumps(out)


def test_native_provider_uses_same_renderer_and_explicit_unsupported():
    m = machine()
    j = job(m)
    inf = inflight(j, instruction(m), (0,))
    inf.inputs = [("x", Slot())]
    raw = _python_state(m, None, 1000)
    python = waits(m)
    m.core = "rust"
    missing = waits(m)
    assert "native_wait_state" in missing["unsupported"]
    m.dispatcher._wait_state = lambda job, limit: raw
    native = waits(m)
    assert python["entities"] == native["entities"]
    assert python["relations"] == native["relations"]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, 10_001])
def test_invalid_bounds_rejected(limit):
    with pytest.raises(ValueError, match="limit"):
        waits(machine(), limit=limit)
