"""Requested cuts preserve issued work and reconstruct a bounded sequential continuation."""

import copy
import pickle
import threading
from concurrent.futures import Future

import pytest
from kohakunode import NodeLibrary

from togeari import Inline, Machine, Use
from togeari.core.contracts.digest import digest
from togeari.core.program.decode.instr import Op
from togeari.core.program.execution import observe_execution
from togeari.core.runtime.checkpoints import checkpoint_key
from togeari.core.runtime.retire import checkpoint as cut_engine
from togeari.core.runtime.state import Job, NameMap, Slot, Strand, const
from togeari.stores import MemoryStore
from togeari.testing import SimCall


class CaptureService:
    """No IO: isolate the cut engine from the independently tested artifact service."""

    def __init__(self):
        self.results = []

    def captured(self, request_id, blob, cut):
        self.results.append((request_id, "captured", blob, cut))

    def finished(self, request_id, status, details):
        self.results.append((request_id, status, details))


@pytest.fixture
def setup_cut():
    machines = []

    def build(source):
        machine = Machine(
            program=source, entry="main", bind={"*": Inline()}, store=MemoryStore(), core="python"
        )
        machines.append(machine)
        dispatcher = machine.dispatcher
        service = CaptureService()
        machine.checkpoints.captured = service.captured
        machine.checkpoints.finished = service.finished
        entry = machine.code.entries["main"]
        job = Job("job", entry, {})
        observation = observe_execution(entry.region, machine.code, machine)
        job.stats.update(
            execution=copy.deepcopy(observation["manifest"]), execution_epoch=observation["epoch"]
        )
        job.inputs_dig = digest({})
        job.add_strand(Strand(job, entry.addr, (), 0, (), NameMap()))
        dispatcher.jobs[job.id] = job
        dispatcher.keys.add(job.key)
        return machine, job, service

    yield build
    for machine in machines:
        machine.dispatcher.jobs.clear()
        machine.stop()


def test_ready_initial_cut_has_no_fetch_or_effect_and_preserves_position(setup_cut):
    machine, job, service = setup_cut("@def ()main(y):\n    (1)identity(y)\n")
    old_strand = next(iter(job.strands.values()))
    result = machine.dispatcher.begin_checkpoint(job.id, "r", None, 0)
    assert result["status"] == "pending"
    [(request, status, blob, cut)] = service.results
    assert (request, status) == ("r", "captured")
    envelope = pickle.loads(blob)
    assert envelope["state"]["prefix"] == () and envelope["state"]["counter"] == 0
    assert envelope["state"]["job_execution_id"] == job.job_execution_id
    assert cut["steps"] == 0 and cut["frontier"] is None
    assert job.cut is None and not job.stats.get("resumed")
    assert next(iter(job.strands.values())).pc == old_strand.pc


def test_paused_boundary_preserves_pause_but_paused_pending_state_needs_resume(setup_cut):
    machine, job, service = setup_cut("@def ()main(y):\n    (1)identity(y)\n")
    job.paused = True
    machine.dispatcher.begin_checkpoint(job.id, "paused", None, 5)
    assert service.results[0][1] == "captured" and job.paused
    service.results.clear()
    job.paused = False
    machine.dispatcher.front.advance(job)
    job.paused = True
    result = machine.dispatcher.begin_checkpoint(job.id, "pending", None, 5)
    assert result["status"] == "needs_resume" and job.paused
    assert service.results == []


def test_hard_fetch_gate_precedes_oldest_progress_exemption(setup_cut):
    source = "@def ()main(y):\n    y = 0\n    ()jump(`loop`)\n    loop:\n        ()jump(`loop`)\n"
    machine, job, _ = setup_cut(source)
    strand = next(iter(job.strands.values()))
    machine.checkpoints.captured = lambda *_: None
    machine.dispatcher.begin_checkpoint(job.id, "r", None, 1)
    assert job.steps == 0
    assert strand.next_key() == (0,)


def test_pending_names_obey_deadline_and_request_cancel_without_unpausing(setup_cut):
    machine, job, service = setup_cut("@def ()main(y):\n    y = 1\n")
    strand = next(iter(job.strands.values()))
    strand.names.set("pending", Slot())
    now = [10.0]
    machine.dispatcher.clock = lambda: now[0]
    machine.dispatcher.begin_checkpoint(job.id, "timeout", 11.0, 5)
    assert job.cut is not None and machine.dispatcher.front.blocked_by_caps(strand)
    assert machine.dispatcher.checkpoint_deadline() == 11.0
    machine.dispatcher.front.advance(job)
    assert job.steps == 0
    now[0] = 11.0
    machine.dispatcher.progress_checkpoint(job)
    assert service.results[-1][1] == "timeout" and job.cut is None
    machine.dispatcher.begin_checkpoint(job.id, "cancel", None, 5)
    assert machine.dispatcher.cancel_checkpoint("cancel")["status"] == "cancelled"
    assert service.results[-1][1] == "cancelled" and not job.paused


def test_control_only_required_prefix_exhausts_explicit_budget(setup_cut):
    source = "@def ()main(y):\n    y = 0\n    ()jump(`loop`)\n    loop:\n        ()jump(`loop`)\n"
    machine, job, service = setup_cut(source)
    job.checkpoint_watermark = (100000,)
    machine.dispatcher.begin_checkpoint(job.id, "budget", None, 3)
    strand = next(iter(job.strands.values()))
    machine.dispatcher.front.run(strand, 5)
    assert service.results[-1] == ("budget", "budget_exhausted", {"steps": 3})
    assert job.cut is None and not job.ended


def test_spawner_is_exposed_once_without_executing_host_calls(setup_cut):
    source = (
        "@def ()main(y):\n    y = 0\n    ([1, 2])each(item, `body`)\n"
        "    body:\n        (item)identity(y)\n"
    )
    machine, job, service = setup_cut(source)
    strand = next(iter(job.strands.values()))
    machine.dispatcher.front.run(strand, 2)
    assert any(st.spawn is not None for st in job.strands.values())
    machine.dispatcher.begin_checkpoint(job.id, "spawn", None, 1)
    assert service.results[0][1] == "captured"
    state = pickle.loads(service.results[0][2])["state"]
    assert state["values"]["item"] == 1
    assert state["frames"][0]["i"] == 0
    assert service.results[0][3]["steps"] == 1
    assert job.stats["calls"] == 0 and not job.window


def test_encoding_deadline_is_rechecked_before_any_destructive_collapse(setup_cut, monkeypatch):
    machine, job, service = setup_cut("@def ()main(y):\n    y = 1\n")
    original = next(iter(job.strands.values()))
    now = [10.0]
    machine.dispatcher.clock = lambda: now[0]
    encode = cut_engine.encode_snapshot

    def slow_encode(*args):
        blob = encode(*args)
        now[0] = 12.0
        return blob

    monkeypatch.setattr(cut_engine, "encode_snapshot", slow_encode)
    machine.dispatcher.begin_checkpoint(job.id, "encoding", 11.0, 10)
    assert service.results == [("encoding", "timeout", {})]
    assert next(iter(job.strands.values())) is original
    assert not job.stats.get("resumed")


def test_local_handler_squashing_neverissued_done_fault_does_not_interrupt_cut(setup_cut):
    source = (
        "@def ()main(y):\n    flag = True\n    @try:\n        (1, 0)floordiv(value)\n"
        "    @except error:\n        flag = True\n        value = 7\n"
        "    (flag)branch(`yes`, `no`)\n    yes:\n        y = value\n    no:\n        y = 0\n"
    )
    machine, job, service = setup_cut(source)
    dispatcher = machine.dispatcher
    # Deliberately simulate an invalid host-produced predicate without declaring
    # a younger executed node: the branch itself fails before submission.
    entry_assignment = machine.code.mem[job.entry.addr]
    assert entry_assignment.op == Op.ASSIGN
    entry_assignment.operand = ("l", 1)
    dispatcher.front.advance(job)
    younger = next(x for x in job.window if x.control and x.fault is not None)
    assert younger.state == "done" and not younger.attempt_ids
    assert isinstance(younger.fault["error"]["message"], str)
    dispatcher.begin_checkpoint(job.id, "local", None, 100)
    assert job.cut is not None
    for _ in range(10):
        dispatcher.issue()
        dispatcher.retire(job)
        if service.results:
            break
        dispatcher.front.advance(job)
        dispatcher.progress_checkpoint(job)
    assert service.results and service.results[0][1] == "captured", service.results


def test_serialization_failure_does_not_collapse_pending_work(setup_cut):
    machine, job, service = setup_cut("@def ()main(y):\n    y = 1\n")
    strand = next(iter(job.strands.values()))
    strand.names.set("lock", const(threading.Lock()))
    machine.dispatcher.begin_checkpoint(job.id, "bad", None, 10)
    assert service.results[-1][1] == "failed"
    assert next(iter(job.strands.values())) is strand
    assert job.cut is None and not job.stats.get("resumed")


class BlockingCall:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def __call__(self, value):
        self.calls.append(value)
        self.started.set()
        assert self.release.wait(5)
        return value


def on_dispatcher(machine, callback):
    result = Future()

    def run():
        try:
            result.set_result(callback())
        except BaseException as exc:  # noqa: BLE001
            result.set_exception(exc)

    machine.dispatcher.inbox.put(("call", run))
    return result.result(5)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_waiting_request_does_not_block_other_jobs_or_busy_spin(core):
    library = NodeLibrary("other_jobs")
    block = BlockingCall()
    library.add("work", block, outputs=["value"])
    source = (
        "@def (x: int)main(result):\n    (x)work(result)\n"
        "@def (x: int)fast(result):\n    (x, 1)add(result)\n"
    )
    machine = Machine(
        programs={source: ["main", "fast"]},
        entry="main",
        libraries=[library],
        bind={
            "work": Use(SimCall(name="work", latency_ms=0), binding_revision="work/1"),
            "kir.std.*": Inline(),
        },
        store=MemoryStore(),
        core=core,
    )
    try:
        handle = machine.submit({"x": 3}, key="slow")
        assert block.started.wait(5)
        jobid = on_dispatcher(machine, lambda: next(iter(machine.dispatcher.jobs)))
        reply = machine.command("checkpoint_job", id=jobid, timeout_s=5, step_budget=1000)
        on_dispatcher(machine, lambda: None)
        fast = machine.submit({"x": 10}, key="fast", entry="fast").result(5)
        assert fast.ok and fast.outputs == {"result": 11}
        if core == "python":
            timeout = on_dispatcher(machine, machine.dispatcher.next_timeout)
            assert timeout is None or timeout > 0
        block.release.set()
        assert reply.result(5)["status"] == "persisted"
        assert handle.result(5).ok
    finally:
        block.release.set()
        machine.stop()


@pytest.mark.parametrize("younger_effect", [False, True])
@pytest.mark.parametrize("core", ["python", "rust"])
def test_fault_interrupted_only_when_fault_discards_an_issued_younger_call(younger_effect, core):
    library = NodeLibrary("cut_fault")
    started, release = threading.Event(), threading.Event()
    effects = []

    @library.node(outputs=["result"])
    def fail(value: int) -> int:
        started.set()
        assert release.wait(5)
        raise ValueError("expected")

    @library.node(outputs=["result"])
    def younger(value: int) -> int:
        effects.append(value)
        return value

    suffix = "    (x)younger(result)\n" if younger_effect else "    (result, 1)add(result)\n"
    source = (
        "@def (x: int)main(result):\n"
        "    @try:\n"
        "        (x)fail(result)\n"
        "    @except error:\n"
        "        result = 7\n"
    ) + suffix
    machine = Machine(
        program=source,
        entry="main",
        libraries=[library],
        bind={
            "fail": Use(SimCall(name="fail", latency_ms=0), binding_revision="fail/1"),
            "younger": Inline(binding_revision="younger/1"),
            "kir.std.*": Inline(),
        },
        store=MemoryStore(),
        core=core,
        record=1.0,
    )
    try:
        handle = machine.submit({"x": 3}, key="fault")
        assert started.wait(5)
        jobid = on_dispatcher(machine, lambda: next(iter(machine.dispatcher.jobs)))
        if younger_effect:
            assert effects == [3]  # completed younger result must be invalidated by the fault
        reply = machine.command("checkpoint_job", id=jobid, timeout_s=5, step_budget=1000)
        on_dispatcher(machine, lambda: None)
        release.set()
        outcome = reply.result(5)
        assert outcome["status"] == (
            "fault_interrupted" if younger_effect else "persisted"
        ), outcome
        record = handle.result(5)
        assert record.ok and record.outputs == {"result": 3 if younger_effect else 8}
        if not younger_effect:
            resumed = machine.resume(outcome["checkpoint_id"], {"x": 3}, key="fault").result(5)
            assert resumed.ok and resumed.outputs == record.outputs
    finally:
        release.set()
        machine.stop()


@pytest.mark.parametrize("window", [1, 64])
@pytest.mark.parametrize("core", ["python", "rust"])
def test_carried_each_cuts_inside_iteration_and_resumes_remaining_iterations(window, core):
    library = NodeLibrary("carried_cut")
    started, release = threading.Event(), threading.Event()
    calls = []

    @library.node(outputs=["total"])
    def work(total: int, item: int) -> int:
        calls.append(item)
        if item == 1:
            started.set()
            assert release.wait(5)
        return total + item

    source = (
        "@def ()main(total):\n    total = 0\n    ([1, 2, 3])each(item, `body`)\n"
        "    body:\n        (total, item)work(total)\n"
    )
    store = MemoryStore()
    machine = Machine(
        program=source,
        entry="main",
        libraries=[library],
        bind={"work": Use(SimCall(name="work", latency_ms=0), binding_revision="work/1")},
        store=store,
        record=1.0,
        window=window,
        core=core,
    )
    try:
        handle = machine.submit({}, key="job")
        assert started.wait(5)
        jobid = on_dispatcher(machine, lambda: next(iter(machine.dispatcher.jobs)))
        reply = machine.command("checkpoint_job", id=jobid, timeout_s=5, step_budget=1000)
        on_dispatcher(machine, lambda: None)
        release.set()
        outcome = reply.result(5)
        assert outcome["status"] == "persisted", outcome
        artifact = pickle.loads(store.get(checkpoint_key(outcome["checkpoint_id"])))
        saved = pickle.loads(artifact["snapshot"])["state"]
        assert saved["frames"] and any(frame["kind"] == "iter" for frame in saved["frames"])
        assert len(saved["record"]) == 1
        original = handle.result(5)
        assert original.ok and original.outputs == {"total": 6}
        assert calls == [1, 2, 3]
        resumed = machine.resume(outcome["checkpoint_id"], {}, key="job").result(5)
        assert resumed.ok and resumed.outputs == original.outputs
        assert calls == [1, 2, 3, 2, 3]  # first issued call is never repeated
    finally:
        release.set()
        machine.stop()


@pytest.mark.parametrize("shape", ["linear", "branch", "each", "try", "collect"])
@pytest.mark.parametrize("core", ["python", "rust"])
def test_request_drains_actual_call_then_resume_never_reexecutes_prefix(shape, core):
    library = NodeLibrary("cut_live")
    block = BlockingCall()
    library.add("work", block, outputs=["value"])
    source = {
        "linear": "@def (x: int)main(result):\n    (x)work(value)\n    (value, 1)add(result)\n",
        "branch": (
            "@def (x: int)main(result):\n    (x)work(value)\n    (value, 0)greater(ok)\n"
            "    (ok)branch(`yes`, `no`)\n    yes:\n        (value, 1)add(result)\n"
            "    no:\n        result = 0\n"
        ),
        "each": (
            "@def (x: int)main(result):\n    result = 0\n    ([1, 2])each(item, `body`)\n"
            "    body:\n        (x)work(value)\n        (result, item)add(result)\n"
        ),
        "try": (
            "@def (x: int)main(result):\n    @try:\n        (x)work(value)\n"
            "        (value, 1)add(result)\n    @except err:\n        result = 0\n"
        ),
        "collect": (
            "@def (x: int)main(result):\n    ([1, 2])collect(item, `body`, value -> result)\n"
            "    body:\n        (x)work(value)\n"
        ),
    }[shape]
    store = MemoryStore()
    machine = Machine(
        program=source,
        entry="main",
        libraries=[library],
        bind={
            "work": Use(SimCall(name="work", latency_ms=0), binding_revision="work/1"),
            "kir.std.*": Inline(),
        },
        store=store,
        record=1.0,
        core=core,
    )
    try:
        handle = machine.submit({"x": 3}, key="job")
        assert block.started.wait(5)
        jobid = on_dispatcher(machine, lambda: next(iter(machine.dispatcher.jobs)))
        reply = machine.command("checkpoint_job", id=jobid, timeout_s=5, step_budget=1000)
        on_dispatcher(machine, lambda: None)
        block.release.set()
        outcome = reply.result(5)
        assert outcome["status"] == "persisted", outcome
        original = handle.result(5)
        expected = [3, 3] if shape == "collect" else 3 if shape == "each" else 4
        assert original.ok and original.outputs == {"result": expected}
        count = 2 if shape in ("each", "collect") else 1
        assert block.calls == [3] * count
        resumed = machine.resume(outcome["checkpoint_id"], {"x": 3}, key="job").result(5)
        assert resumed.ok and resumed.outputs == original.outputs
        assert block.calls == [3] * count
        assert resumed.stats["job_execution_id"] == original.stats["job_execution_id"]
    finally:
        block.release.set()
        machine.stop()
