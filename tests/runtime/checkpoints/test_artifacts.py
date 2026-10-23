"""Pinned artifacts, acknowledged persistence and explicit-resume admission without cut timing."""

import json
import pickle
import queue
import subprocess
import sys
import threading
from functools import partial
from types import SimpleNamespace

import pytest
from kohakunode import NodeLibrary

from togeari import BasePlugin, Inline, Machine, SourceItem, Use
from togeari.core.contracts.digest import digest
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.io import MISS
from togeari.core.contracts.results import Ok
from togeari.core.runtime.checkpoints import (
    HISTORY_LIMIT,
    IO_QUEUE_SIZE,
    IO_WORKERS,
    CheckpointManager,
    CheckpointResumeError,
    checkpoint_key,
)
from togeari.core.runtime.handles import JobHandle
from togeari.core.runtime.retire.codec import encode_snapshot
from togeari.core.runtime.retire.snapshot import snapshot_key
from togeari.core.runtime.state import Job
from togeari.io import Memory
from togeari.stores import MemoryStore

SOURCE = "@def (x: int)main(y):\n    (x)identity(y)\n"


class Dispatcher:
    """Drive store acknowledgements on the test thread, as the dispatcher inbox would."""

    def __init__(self):
        self.inbox = queue.Queue()
        self.cancelled = []
        self.requested = []

    def clock(self):
        return 100.0

    def begin_checkpoint(self, jobid, requestid, deadline, step_budget):
        self.requested.append((jobid, requestid, deadline, step_budget))
        return {"status": "pending"}

    def cancel_checkpoint(self, requestid):
        self.cancelled.append(requestid)

    def until(self, predicate):
        for _ in range(100):
            if predicate():
                return
            kind, callback = self.inbox.get(timeout=5)
            assert kind == "call"
            callback()
        raise AssertionError("checkpoint callback did not settle")


class ObservedStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.threads = []

    def get(self, key):
        self.threads.append(("get", threading.get_ident()))
        return super().get(key)

    def put(self, key, value):
        self.threads.append(("put", threading.get_ident()))
        return super().put(key, value)

    def delete(self, key):
        self.threads.append(("delete", threading.get_ident()))
        return super().delete(key)


class HeldStore(ObservedStore):
    def __init__(self, failure=False):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.failure = failure

    def put(self, key, value):
        self.entered.set()
        assert self.release.wait(5), "test did not release store write"
        if self.failure:
            raise OSError("store rejected write")
        return super().put(key, value)


class DeferredCall(EngineBase):
    """Public engine handshake: tests choose when actual submitted work completes."""

    def __init__(self):
        super().__init__("checkpoint-work")
        self.submissions = queue.Queue()
        self.count = 0
        self.cancelled = []

    def capacity(self):
        return 8

    def submit(self, item, done):
        self.count += 1
        self.submissions.put((self.count, item.inputs["x"], done))
        return self.count

    def cancel(self, ticket):
        self.cancelled.append(ticket)


def live_machine(core, store):
    library = NodeLibrary("artifact_lifecycle")
    library.add("work", lambda x: x, outputs=["value"])
    engine = DeferredCall()
    source = (
        "@def (x: int)main(result):\n    (x)work(value)\n    (value, 1)add(result)\n"
        "@def (x: int)fast(result):\n    (x, 1)add(result)\n"
    )
    machine = Machine(
        programs={source: ["main", "fast"]},
        entry="main",
        libraries=[library],
        bind={"work": Use(engine, binding_revision="work/1"), "kir.std.*": Inline()},
        core=core,
        store=store,
        record=1.0,
    )
    return machine, engine


class ReadStore(MemoryStore):
    def __init__(self, *, blocked=False):
        super().__init__()
        self.blocked = blocked
        self.entered, self.release = threading.Event(), threading.Event()

    def get(self, key):
        self.entered.set()
        if self.blocked:
            assert self.release.wait(10), "test did not release checkpoint read"
        raise OSError("checkpoint read rejected")


@pytest.mark.parametrize("core", ["python", "rust"])
def test_real_request_captures_before_ack_and_other_jobs_progress_then_resume(core):
    store = HeldStore()
    machine, engine = live_machine(core, store)
    completion = None
    try:
        handle = machine.submit({"x": 3}, key="original")
        ticket, value, done = engine.submissions.get(timeout=5)
        completion = partial(done, ticket, [Ok(value, True)])
        [job] = machine.inspect("jobs").result(5)
        reply = machine.command("checkpoint_job", id=job["id"], timeout_s=10)
        [pending] = machine.inspect("checkpoints", job=job["id"]).result(5)
        assert pending["status"] == "draining"
        completion()
        completion = None
        assert store.entered.wait(5), "real cut did not reach artifact persistence"
        assert not reply.done() and machine.checkpoints.persisted == 0
        original = handle.result(5)
        assert original.ok and original.outputs == {"result": 4}
        assert engine.count == 1
        fast = machine.submit({"x": 8}, key="other", entry="fast").result(5)
        assert fast.ok and fast.outputs == {"result": 9}
        assert not store.release.is_set()  # store IO never held the dispatcher
        [pending] = machine.inspect("checkpoints", checkpoint_id=pending["checkpoint_id"]).result(5)
        assert pending["status"] == "persisting"
        store.release.set()
        persisted = reply.result(5)
        assert persisted["status"] == "persisted" and machine.checkpoints.persisted == 1
        token = persisted["checkpoint_id"]
        artifact = pickle.loads(store.get(checkpoint_key(token)))
        saved = pickle.loads(artifact["snapshot"])["state"]
        assert saved["record"] and saved["stats"]["record_invocations"]
        resumed = machine.resume(token, {"x": 3}, key="original").result(5)
        assert resumed.ok and resumed.outputs == original.outputs and engine.count == 1
        assert resumed.stats["job_execution_id"] == original.stats["job_execution_id"]
        assert resumed.record[: len(saved["record"])] == saved["record"]
        assert (
            resumed.stats["record_invocations"][: len(saved["record"])]
            == saved["stats"]["record_invocations"]
        )
        assert store.get(checkpoint_key(token)) is not MISS
        deletion = machine.command("delete_checkpoint", checkpoint_id=token).result(5)
        assert deletion["status"] == "deleted" and store.get(checkpoint_key(token)) is MISS
    finally:
        store.release.set()
        if completion is not None:
            completion()
        machine.stop(drain=False)


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("phase", ["draining", "persisting"])
def test_public_cancel_releases_request_without_cancelling_job(core, phase):
    store = HeldStore()
    machine, engine = live_machine(core, store)
    completion = None
    try:
        handle = machine.submit({"x": 3}, key="original")
        ticket, value, done = engine.submissions.get(timeout=5)
        completion = partial(done, ticket, [Ok(value, True)])
        [job] = machine.inspect("jobs").result(5)
        reply = machine.command("checkpoint_job", id=job["id"], timeout_s=10)
        [row] = machine.inspect("checkpoints", job=job["id"]).result(5)
        if phase == "persisting":
            completion()
            completion = None
            assert store.entered.wait(5)
        token = row["checkpoint_id"]
        assert machine.command("cancel_checkpoint", checkpoint_id=token).result(5)
        assert reply.result(5)["status"] == (
            "cancelled" if phase == "draining" else "persistence_unknown"
        )
        assert not machine.command("cancel_checkpoint", checkpoint_id=token).result(5)
        if completion is not None:
            completion()
            completion = None
        assert handle.result(5).ok and engine.cancelled == []
        assert machine.submit({"x": 8}, key="other", entry="fast").result(5).ok
        if phase == "draining":
            assert not store.entered.is_set() and store.get(checkpoint_key(token)) is MISS
    finally:
        store.release.set()
        if completion is not None:
            completion()
        machine.stop(drain=False)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_public_missing_store_and_read_error_do_not_fall_back_to_fresh_work(core):
    machine, engine = live_machine(core, None)
    completion = None
    try:
        handle = machine.submit({"x": 3}, key="original")
        ticket, value, done = engine.submissions.get(timeout=5)
        completion = partial(done, ticket, [Ok(value, True)])
        [job] = machine.inspect("jobs").result(5)
        result = machine.command("checkpoint_job", id=job["id"]).result(5)
        assert result["status"] == "failed" and result["reason"] == "no_store"
        completion()
        completion = None
        assert handle.result(5).ok
        failed = machine.resume("a" * 32, {"x": 3}, key="original").result(5)
        assert failed.failure["kind"] == "CheckpointIncompatible" and engine.count == 1
    finally:
        if completion is not None:
            completion()
        machine.stop(drain=False)

    store = ReadStore()
    machine, engine = live_machine(core, store)
    try:
        failed = machine.resume("a" * 32, {"x": 3}, key="original").result(5)
        assert failed.failure["kind"] == "CheckpointIncompatible"
        assert "read rejected" in failed.failure["message"] and engine.count == 0
        assert machine.submit({"x": 8}, key="other", entry="fast").result(5).ok
    finally:
        machine.stop(drain=False)


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("route", ["handle", "source"])
def test_public_stop_cancels_blocked_resume_before_draining_dispatcher(core, route):
    store = ReadStore(blocked=True)
    machine, engine = live_machine(core, store)
    sink, errors = Memory(), []
    runner = None
    try:
        if route == "handle":
            handle = machine.resume("a" * 32, {"x": 3}, key="original")
        else:

            def run():
                try:
                    machine.run([SourceItem("original", {"x": 3}, checkpoint="a" * 32)], sink)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            runner = threading.Thread(target=run, daemon=True)
            runner.start()
        assert store.entered.wait(5)
        assert machine.submit({"x": 8}, key="other", entry="fast").result(5).ok
        machine.stop(drain=True, timeout=1)
        assert not store.release.is_set() and machine.checkpoints.pending_resumes == 0
        assert engine.count == 0
        if route == "handle":
            assert handle.result(1).failure["kind"] == "Cancelled"
        else:
            runner.join(5)
            assert not runner.is_alive() and errors == []
            assert len(sink.records) == 1 and not sink.records[0].ok
    finally:
        store.release.set()
        machine.stop(drain=False)
        if runner is not None:
            runner.join(5)


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("phase", ["draining", "persisting"])
def test_public_stop_settles_cut_or_unknown_write_without_waiting_for_store(core, phase):
    store = HeldStore()
    machine, engine = live_machine(core, store)
    completion = None
    try:
        handle = machine.submit({"x": 3}, key="original")
        ticket, value, done = engine.submissions.get(timeout=5)
        completion = partial(done, ticket, [Ok(value, True)])
        [job] = machine.inspect("jobs").result(5)
        reply = machine.command("checkpoint_job", id=job["id"], timeout_s=10)
        machine.inspect("checkpoints", job=job["id"]).result(5)
        if phase == "persisting":
            completion()
            completion = None
            assert store.entered.wait(5)
            assert handle.result(5).ok
        machine.stop(drain=False, timeout=1)
        assert reply.result(1)["status"] == (
            "cancelled" if phase == "draining" else "persistence_unknown"
        )
        assert not store.release.is_set() and machine.checkpoints.persisted == 0
        if phase == "draining":
            assert handle.result(1).failure["kind"] == "Cancelled"
            assert not store.entered.is_set()
    finally:
        store.release.set()
        if completion is not None:
            completion()
        machine.stop(drain=False)
    for thread in machine.checkpoints._threads:
        thread.join(5)
        assert not thread.is_alive()


@pytest.fixture
def context():
    machine = Machine(program=SOURCE, entry="main", bind={"*": Inline()}, core="python")
    managers = []

    def make(store=None):
        dispatcher = Dispatcher()
        host = SimpleNamespace(
            code=machine.code,
            store=store if store is not None else ObservedStore(),
            dispatcher=dispatcher,
            default_entry="other-default",
            persistence_compatibility=machine.persistence_compatibility,
        )
        # Execution observation needs the actual machine's fixed binding/history context.
        for name, value in vars(machine).items():
            if name not in {"store", "dispatcher", "default_entry", "checkpoints"}:
                setattr(host, name, value)
        manager = CheckpointManager(host)
        managers.append(manager)
        return manager, host, dispatcher

    yield make
    for manager in managers:
        manager.close()
    machine.stop()


def capture(manager, host, *, timeout_s=30):
    entry = host.code.entries["main"]
    job = Job("same", entry, {"x": 1})
    raw = {
        "job_execution_id": job.job_execution_id,
        "pc": entry.addr,
        "prefix": (),
        "counter": 0,
        "frames": [],
        "values": {"x": 1},
        "stats": dict(job.stats),
        "steps": 0,
        "record": [],
    }
    blob = encode_snapshot(raw, entry, host.code, entry.region.execution)
    future = manager.request(7, timeout_s=timeout_s)
    row = manager.query(job=7)[-1]
    token = row["checkpoint_id"]
    cut = {
        "job_key": "same",
        "key": (0,),
        "entry": "main",
        "program_hash": entry.region.semantic_hash,
        "inputs_digest": digest({"x": 1}),
        "job_execution_id": job.job_execution_id,
    }
    manager.captured(token, blob, cut)
    return token, future, job.job_execution_id


def prepare(manager, dispatcher, item, transform=None):
    result = []

    def callback(original):
        assert manager.pending_resumes == 1
        assert not manager.prepare_resume(original, None, callback), "recursive IO staging"
        transformed = transform(original) if transform else original
        try:
            result.append(manager.validate_resume(original, transformed))
        except CheckpointResumeError as exc:
            result.append(exc)
        assert manager.pending_resumes == 1

    assert manager.prepare_resume(item, None, callback)
    dispatcher.until(lambda: bool(result))
    assert manager.pending_resumes == 0
    return result[0]


class StripCheckpoint(BasePlugin):
    name = "strip-checkpoint"

    def pre_admit(self, item):
        return SourceItem(item.key, item.inputs, item.entry)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_public_resume_and_source_path_preserve_uid_and_fail_closed(context, core):
    manager, host, dispatcher = context()
    token, future, uid = capture(manager, host)
    dispatcher.until(future.done)
    programs = {"@def ()other(value):\n    value = 9\n": ["other"], SOURCE: ["main"]}
    machine = Machine(
        programs=programs,
        entry="other",
        bind={"*": Inline()},
        core=core,
        store=host.store,
        record=1.0,
    )
    try:
        resumed = machine.resume(token, {"x": 1}, key="same").result(5)
        assert resumed.ok and resumed.outputs == {"y": 1}
        assert resumed.entry == "main" and resumed.stats["job_execution_id"] == uid
        assert resumed.stats["resumed"]
        missing = machine.resume("f" * 32, {"x": 1}, key="same").result(5)
        assert not missing.ok and missing.failure["kind"] == "CheckpointIncompatible"
        sink = Memory()
        machine.run([SourceItem("same", {"x": 1}, checkpoint=token)], sink)
        assert len(sink.records) == 1 and sink.records[0].stats["job_execution_id"] == uid
        assert host.store.get(checkpoint_key(token)) is not MISS
        fresh = machine.submit({"x": 1}, key="same", entry="main").result(5)
        assert fresh.ok and fresh.stats["job_execution_id"] != uid
    finally:
        machine.stop()

    stripped = Machine(
        program=SOURCE,
        entry="main",
        bind={"*": Inline()},
        core=core,
        store=host.store,
        plugins=[StripCheckpoint()],
    )
    try:
        record = stripped.resume(token, {"x": 1}, key="same").result(5)
        assert not record.ok and record.failure["kind"] == "CheckpointIncompatible"
    finally:
        stripped.stop()


@pytest.mark.parametrize("core", ["python", "rust"])
def test_pinned_artifact_resumes_in_fresh_process(context, tmp_path, core):
    manager, host, dispatcher = context()
    token, future, uid = capture(manager, host)
    dispatcher.until(future.done)
    path = tmp_path / "checkpoint.pickle"
    path.write_bytes(host.store.get(checkpoint_key(token)))
    script = """
import json
import sys
from pathlib import Path
from togeari import Inline, Machine
from togeari.core.runtime.checkpoints import checkpoint_key
from togeari.stores import MemoryStore
path, token, core, source = sys.argv[1:]
store = MemoryStore()
store.put(checkpoint_key(token), Path(path).read_bytes())
machine = Machine(program=source, entry='main', bind={'*': Inline()}, store=store, core=core)
try:
    record = machine.resume(token, {'x': 1}, key='same').result(5)
    assert record.ok and record.stats['resumed'], record.failure
    print(json.dumps({'uid': record.stats['job_execution_id'], 'outputs': record.outputs}))
finally:
    machine.stop()
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(path), token, core, SOURCE],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout.strip().splitlines()[-1]) == {"uid": uid, "outputs": {"y": 1}}


def test_source_checkpoint_is_appended_and_fresh_admission_never_reads_store(context):
    item = SourceItem("same", {"x": 1}, "main", True)
    assert item.auto_key and item.checkpoint is None
    manager, host, _ = context()
    assert not manager.prepare_resume(item, None, lambda _: pytest.fail("unexpected callback"))
    assert manager.validate_resume(item, item) is None
    assert host.store.threads == [] and manager._threads == []


def test_pinned_artifact_survives_auto_cleanup_and_resumes_identity(context):
    manager, host, dispatcher = context()
    caller_thread = threading.get_ident()
    token, future, uid = capture(manager, host)
    dispatcher.until(future.done)
    assert future.result()["status"] == "persisted" and manager.persisted == 1
    assert manager._requests[token]["work"] is None
    assert host.store.threads[0][0] == "put" and host.store.threads[0][1] != caller_thread
    assert dispatcher.requested[0][2:] == (130.0, 100_000)
    artifact = host.store.get(checkpoint_key(token))
    envelope = pickle.loads(artifact)
    assert envelope["job_key"] == "same" and envelope["cut"]["key"] == (0,)
    auto_key = snapshot_key(envelope["program_hash"], "same", digest({"x": 1}))
    assert auto_key != checkpoint_key(token)
    host.store.put(auto_key, b"later automatic snapshot")
    host.store.delete(auto_key)
    assert host.store.get(checkpoint_key(token)) == artifact
    host.store.threads.clear()
    item = SourceItem("same", {"x": 1}, checkpoint=token)
    restored = prepare(manager, dispatcher, item)
    assert item.entry == "main"  # artifact, not other-default
    assert restored["decision"]["allow"]
    assert restored["state"]["job_execution_id"] == uid
    assert all(thread != caller_thread for _, thread in host.store.threads)
    rows = manager.query(checkpoint_id=token)
    rows[0]["cut"]["key"] = (999,)
    assert manager.query(checkpoint_id=token)[0]["cut"]["key"] == (0,)
    deletion = manager.delete(token)
    dispatcher.until(deletion.done)
    assert deletion.result()["status"] == "deleted"
    assert host.store.get(checkpoint_key(token)) is MISS


@pytest.mark.parametrize("failure", [False, True])
def test_timeout_after_store_entry_is_unknown_and_late_ack_does_not_reset_reply(context, failure):
    store = HeldStore(failure=failure)
    manager, host, dispatcher = context(store)
    token, future, _ = capture(manager, host)
    try:
        assert store.entered.wait(5)
        work = manager._requests[token]["work"]
        assert work.run is None  # payload moved into only the active backend invocation
        assert not future.done() and manager.persisted == 0
        manager._timeout(token)
        first = future.result()
        assert first["status"] == "persistence_unknown"
        assert dispatcher.cancelled == [token]
        assert manager.delete(token).result()["status"] == "busy"
        for _ in range(HISTORY_LIMIT + 2):
            manager.request(7, timeout_s=0)
        assert manager.query(checkpoint_id=token)[0]["status"] == "persistence_unknown"
        assert len(manager.query()) <= HISTORY_LIMIT
    finally:
        store.release.set()
    dispatcher.until(lambda: "persistence_status" in manager.query(checkpoint_id=token)[0])
    assert future.result() == first
    row = manager.query(checkpoint_id=token)[0]
    assert row["persistence_status"] == ("persist_failed" if failure else "persisted")
    assert manager.persisted == (0 if failure else 1)
    assert manager._requests[token]["work"] is None
    assert work.run is None and work.acknowledge is None
    assert len(manager.query()) <= HISTORY_LIMIT


def test_store_failure_is_not_persisted(context):
    store = HeldStore(failure=True)
    store.release.set()
    manager, host, dispatcher = context(store)
    _, future, _ = capture(manager, host)
    dispatcher.until(future.done)
    assert future.result()["status"] == "persist_failed"
    assert manager.persisted == 0


def test_cancel_cut_is_not_cancel_job_and_no_write_can_follow(context):
    manager, host, dispatcher = context()
    future = manager.request(7)
    token = manager.query()[0]["checkpoint_id"]
    assert manager.cancel(token)
    assert future.result()["status"] == "cancelled"
    assert dispatcher.cancelled == [token]
    manager.captured(token, b"not a snapshot", {})
    assert host.store.threads == []


@pytest.mark.parametrize("change", ["inputs", "key", "entry", "strip", "replace-strip"])
def test_resume_validates_after_pre_admit_transform_without_fresh_fallback(context, change):
    manager, host, dispatcher = context()
    token, future, _ = capture(manager, host)
    dispatcher.until(future.done)

    def transform(item):
        match change:
            case "inputs":
                item.inputs["x"] = 2
            case "key":
                item.key = "other"
            case "entry":
                item.entry = "other"
            case "strip":
                item.checkpoint = None
            case "replace-strip":
                return SourceItem(item.key, item.inputs, item.entry)
        return item

    result = prepare(manager, dispatcher, SourceItem("same", {"x": 1}, checkpoint=token), transform)
    assert isinstance(result, CheckpointResumeError)


@pytest.mark.parametrize("payload", [None, b"not a pickle", pickle.dumps({"schema": "wrong"})])
def test_missing_or_bad_artifact_reports_error_through_original_callback(context, payload):
    manager, host, dispatcher = context()
    token = "a" * 32
    if payload is not None:
        host.store.put(checkpoint_key(token), payload)
    item = SourceItem("same", {"x": 1}, checkpoint=token)
    result = prepare(manager, dispatcher, item)
    assert isinstance(result, CheckpointResumeError)
    assert not manager._staged


def test_zero_timeout_and_invalid_limits_never_begin_cut(context):
    manager, _, dispatcher = context()
    assert manager.request(7, timeout_s=0).result()["status"] == "timeout"
    assert dispatcher.requested == []
    for timeout in (True, -1, float("inf"), float("nan"), "1"):
        with pytest.raises(ValueError):
            manager.request(7, timeout_s=timeout)
    for budget in (True, -1, 1.5):
        with pytest.raises(ValueError):
            manager.request(7, step_budget=budget)


def test_queue_bound_and_shutdown_do_not_wait_for_hung_store(context):
    store = HeldStore()
    manager, host, dispatcher = context(store)
    release = store.release
    tokens = []
    futures = []
    try:
        for _ in range(IO_WORKERS):
            token, future, _ = capture(manager, host)
            tokens.append(token)
            futures.append(future)
        assert store.entered.wait(5)
        for _ in range(IO_QUEUE_SIZE + IO_WORKERS):
            token, future, _ = capture(manager, host)
            tokens.append(token)
            futures.append(future)
        assert any(f.done() and f.result().get("reason") == "io_queue_full" for f in futures)
        assert manager._queue.qsize() <= IO_QUEUE_SIZE
        manager.close()  # must return before release, without joining a blocked daemon
        assert not release.is_set()
        assert all(f.done() for f in futures)
    finally:
        release.set()
    assert len(manager._threads) == IO_WORKERS + 1
    for thread in manager._threads:
        thread.join(5)
        assert not thread.is_alive(), "responsive checkpoint worker survived close"
    assert len(store.threads) <= IO_WORKERS  # queued writes were cancelled, not drained to store


def test_stale_read_ack_and_timer_cannot_consume_repeated_source_item(context):
    manager, host, _ = context()
    entered, release = threading.Event(), threading.Event()

    def blocked_get(key):
        entered.set()
        assert release.wait(5)
        return MISS

    host.store.get = blocked_get
    item = SourceItem("same", {"x": 1}, checkpoint="a" * 32)
    answers = []

    def callback(original):
        with pytest.raises(CheckpointResumeError):
            manager.validate_resume(original, original)
        answers.append("rejected")

    try:
        manager.prepare_resume(item, None, callback)
        assert entered.wait(5)
        first_generation = manager._resumes[id(item)]["generation"]
        manager._resume_timeout(id(item), first_generation)
        assert answers == ["rejected"]
        manager.prepare_resume(item, None, callback)
        second_generation = manager._resumes[id(item)]["generation"]
        assert first_generation != second_generation
        manager._resume_loaded(id(item), MISS, None, first_generation)
        manager._resume_timeout(id(item), first_generation)
        assert answers == ["rejected"] and manager.pending_resumes == 1
        assert manager._resumes[id(item)]["generation"] == second_generation
        manager._resume_timeout(id(item), second_generation)
        assert answers == ["rejected", "rejected"] and manager.pending_resumes == 0
    finally:
        release.set()


def test_repeated_pending_source_object_gets_two_admission_callbacks(context):
    manager, host, dispatcher = context()
    entered, release = threading.Event(), threading.Event()

    def blocked_get(key):
        entered.set()
        assert release.wait(5)
        return MISS

    host.store.get = blocked_get
    item = SourceItem("same", {"x": 1}, checkpoint="a" * 32)
    seen = []

    def callback(original):
        with pytest.raises(CheckpointResumeError):
            manager.validate_resume(original, original)
        seen.append(original)

    try:
        assert manager.prepare_resume(item, None, callback)
        assert entered.wait(5)
        assert manager.prepare_resume(item, None, callback)
        assert manager.pending_resumes == 2
    finally:
        release.set()
    dispatcher.until(lambda: len(seen) == 2)
    assert seen[0] is not seen[1] and all(it.key == "same" for it in seen)
    assert manager.pending_resumes == 0


def test_weak_resume_inputs_are_rejected_without_store_read(context):
    manager, host, dispatcher = context()
    item = SourceItem("same", {"x": threading.Lock()}, checkpoint="a" * 32)
    result = prepare(manager, dispatcher, item)
    assert isinstance(result, CheckpointResumeError)
    assert "strong input digest" in str(result) and host.store.threads == []


def test_shutdown_settles_deferred_programmatic_resume(context):
    store = HeldStore()
    manager, _, _ = context(store)
    entered, release = threading.Event(), threading.Event()

    def blocked_get(key):
        entered.set()
        assert release.wait(5)
        return MISS

    store.get = blocked_get
    handle = JobHandle("same")
    item = SourceItem("same", {"x": 1}, checkpoint="a" * 32)
    try:
        assert manager.prepare_resume(item, handle, lambda _: pytest.fail("closed callback"))
        assert entered.wait(5)
        manager.close()
        assert handle.result(1).failure["kind"] == "Cancelled"
        assert manager.pending_resumes == 0
    finally:
        release.set()
