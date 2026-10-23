"""Deadline heap lifetime and mixed-batch attribution, with no real-clock waiting."""

import pytest
from kohakunode import NodeLibrary

from togeari import Lane, Machine, Use
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.results import ItemTimeout, Ok, UnitError
from togeari.core.contracts.workitem import WorkItem
from togeari.core.lanes.lane import Retry, State
from togeari.core.lanes.native import NativeLane
from togeari.policies import BudgetFormer, SizeOrWait

LIB = NodeLibrary("deadline_lane")


@LIB.node()
def inc(x: int) -> int:
    return x + 1


class Unit(EngineBase):
    protocol = "python"
    max_batch = 8

    def __init__(self, kind="call"):
        super().__init__("controlled")
        self.kind = kind
        self.submitted = []
        self.cancelled = []

    def capacity(self):
        return 1

    def submit(self, items, done):
        self.submitted.append(items)
        return len(self.submitted)

    def cancel(self, ticket):
        self.cancelled.append(ticket)


def prepare(core, *, attempt=None, kind="call", retry=None, former=None):
    unit = Unit(kind)
    lane = Lane(
        [unit],
        timeout_s=attempt,
        retry=retry,
        former=former if former is not None else SizeOrWait(2, 0) if kind == "batch" else None,
    )
    machine = Machine(program="(1)inc(x)", libraries=[LIB], bind={"inc": Use(lane)}, core=core)
    lane.clock = lambda: 0.0
    assert isinstance(lane, NativeLane) == (core == "rust")
    return machine, lane, unit


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("crossing", ["estimate", "key"])
@pytest.mark.parametrize("mixed", [False, True])
def test_post_transform_deadline_boundary_has_no_attempt_and_preserves_live_batch(
    core, crossing, mixed
):
    clock = [0.0]
    prepared = []

    def estimate(item):
        if prepared and crossing == "estimate":
            clock[0] = 1.25
        return {"tokens": 1}

    former = BudgetFormer({"tokens": 4}, estimate, size=4, wait_ms=0)
    machine, lane, unit = prepare(core, attempt=2, kind="batch", former=former)
    lane.clock = lambda: clock[0]

    def key(item):
        if prepared and crossing == "key":
            clock[0] = 1.25
        return "same"

    def hook(lane, unit, items):
        assert clock[0] == 0
        prepared.append(True)
        return items

    unit.key = key
    lane.hook_submit = hook
    expired = work(1, 1)
    live = work(10, 10)
    try:
        lane.enqueue(expired, 0)
        if mixed:
            lane.enqueue(live, 0)
        lane.pump(0)
        assert prepared and clock[0] == 1.25
        assert expired.attempt_id is None
        assert unit.submitted == ([[live]] if mixed else [])
        [(got, result)] = lane.timers(clock[0])
        assert got is expired and isinstance(result.error, ItemTimeout)
        assert expired.ext["policy_failure"]["scope"] == "call"
        assert lane.resource_view()["total"] == {"tokens": float(mixed)}
        if mixed:
            assert live.attempt_id is not None
            assert live.ext["sent"] == 1.25
            assert lane.next_deadline(clock[0]) == 3.25
            [submission] = lane.subs.values()
            submission = submission.id if isinstance(lane, NativeLane) else submission
            assert lane.results(submission, [Ok(2)], 1.5) == [(live, Ok(2))]
        assert lane.resource_view()["total"] == {"tokens": 0.0}
        assert lane.inflight == 0 and lane.queued() == 0
        assert lane.timers(20) == []
    finally:
        machine.stop()


def work(cap=None, timeout=None):
    item = WorkItem("inc", {"x": 1}, None, deadline=cap)
    if timeout is not None:
        item.ext.update(call_timeout_s=timeout, policy_source="binding")
    return item


@pytest.mark.parametrize("core", ["python", "rust"])
def test_original_cap_survives_inflight_heap_cleanup_retry_and_backoff(core):
    machine, lane, unit = prepare(core, attempt=2, retry=Retry(max=2, backoff_ms=2000))
    try:
        item = work(1, 1)
        lane.enqueue(item, 0)
        lane.pump(0)
        assert lane.next_deadline(0.1) == 1
        [submission] = lane.subs.values()
        submission = submission.id if isinstance(lane, NativeLane) else submission
        assert lane.results(submission, [UnitError(ConnectionError("lost"))], 0.25) == []
        assert lane.timers(0.75) == []
        [(got, result)] = lane.timers(1)
        assert got is item and isinstance(result.error, ItemTimeout)
        assert item.ext["policy_failure"] == {
            "reason": "deadline",
            "scope": "call",
            "timeout_s": 1,
            "source": "binding",
        }
        lane.timers(4)
        lane.pump(4)
        assert len(unit.submitted) == 1
        assert lane.queued() == 0
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("live_error", [False, True])
def test_expired_batch_item_keeps_unit_failure_health_and_live_batchmate(core, live_error):
    machine, lane, unit = prepare(core, kind="batch", retry=Retry(max=2, backoff_ms=500))
    try:
        expired, live = work(1, 1), work(3, 3)
        lane.enqueue(expired, 0)
        lane.enqueue(live, 0)
        lane.pump(0)
        [submission] = lane.subs.values()
        submission = submission.id if isinstance(lane, NativeLane) else submission
        result = UnitError(ConnectionError("lost"))
        outputs = lane.results(submission, [result, result if live_error else Ok(2)], 1)
        assert lane.state[id(unit)].state == State.DOWN
        assert outputs[0][0] is expired and isinstance(outputs[0][1].error, ItemTimeout)
        assert expired.ext["policy_failure"]["scope"] == "call"
        assert "policy_failure" not in live.ext
        if live_error:
            assert len(outputs) == 1 and live.ext["retries"] == 1
        else:
            assert outputs[1] == (live, Ok(2))
            assert lane.state[id(unit)].items_done == 1
        assert lane.timers(1) == []
        assert lane.results(submission, [Ok(2), Ok(2)], 2) == []
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_timer_expiry_once_cancels_and_late_completion_cannot_succeed(core):
    machine, lane, unit = prepare(core, attempt=0.5)
    try:
        item = work(1, 1)
        lane.enqueue(item, 0)
        lane.pump(0)
        [submission] = lane.subs.values()
        submission = submission.id if isinstance(lane, NativeLane) else submission
        [(got, result)] = lane.timers(0.5)
        assert got is item and isinstance(result.error, ItemTimeout)
        assert item.ext["policy_failure"] == {
            "reason": "deadline",
            "scope": "attempt",
            "timeout_s": 0.5,
            "source": "lane",
        }
        assert unit.cancelled == [1]
        assert lane.results(submission, [Ok(2)], 0.75) == []
        assert lane.timers(1) == []
    finally:
        machine.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_manual_deadline_has_unknown_duration_and_expired_queue_never_submits(core):
    machine, lane, unit = prepare(core)
    try:
        item = work(0.5)
        lane.enqueue(item, 0)
        lane.pump(0.5)
        assert unit.submitted == []
        [(got, result)] = lane.timers(0.5)
        assert got is item and isinstance(result.error, ItemTimeout)
        assert item.ext["policy_failure"]["source"] == "work_item"
        assert item.ext["policy_failure"]["timeout_s"] is None
        assert lane.queued() == 0
    finally:
        machine.stop()
