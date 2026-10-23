"""Inspection/archive projections never invent invocation or attempt identities."""

import copy
import pickle
import re
import threading
from types import SimpleNamespace

import pytest
from kohakunode import NodeLibrary

from togeari import Inline, Machine, Use
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.io import JobRecord
from togeari.core.contracts.results import Ok
from togeari.core.plugin.base import BasePlugin, PluginContext
from togeari.core.plugin.tables import q_job, q_jobs
from togeari.core.runtime.identity import record_invocation
from togeari.core.runtime.state import Inflight, Job
from togeari.plugins.record.archive import Archive, ArchiveWriter
from togeari.plugins.report.spec import machine_spec
from togeari.stores import MemoryStore
from togeari.testing.sim import SimEngine


class HeldCall(EngineBase):
    """Handshake-controlled engine: inspection occurs while one actual call is in flight."""

    def __init__(self):
        super().__init__("held")
        self.submitted = threading.Event()
        self.pending = None
        self.identity = None

    def capacity(self):
        return 1 if self.pending is None else 0

    def submit(self, item, done):
        self.identity = (item.job_execution_id, item.invocation_id, item.attempt_id)
        self.pending = (item, done)
        self.submitted.set()
        return 1

    def finish(self):
        if self.pending is not None:
            _, done = self.pending
            self.pending = None
            done(1, [Ok(-1)])


@pytest.mark.parametrize("core", ["python", "rust"])
def test_live_job_query_identity_matches_actual_engine_submission(core):
    if core == "rust":
        pytest.importorskip("togeari._rs")
    engine = HeldCall()
    machine = Machine(
        program="(1)neg(result)\n",
        bind={"neg": Use(engine)},
        core=core,
        record=1.0,
    )
    try:
        future = machine.submit({}, key="same-source-key")
        assert engine.submitted.wait(5), "engine never received work"
        [resident] = machine.inspect("jobs").result(5)
        detail = machine.inspect("job", id=resident["id"]).result(5)
        [row] = [w for w in detail["window"] if w["node"] == "neg"]
        job_id, invocation, attempt = engine.identity
        assert resident["job_execution_id"] == detail["job_execution_id"] == job_id
        assert re.fullmatch(r"[0-9a-f]{32}", job_id)
        assert row["invocation_id"] == invocation and re.fullmatch(r"[0-9a-f]{64}", invocation)
        assert row["attempt_ids"] == [attempt] and re.fullmatch(r"[0-9a-f]{32}", attempt)
        assert row["resolution"] == "executed"
        row["attempt_ids"].append("spoof")
        again = machine.inspect("job", id=resident["id"]).result(5)
        assert next(w for w in again["window"] if w["node"] == "neg")["attempt_ids"] == [attempt]
        engine.finish()
        record = future.result(5)
        assert record.stats["job_execution_id"] == job_id
        assert record.stats["record_invocations"][0] == {
            "invocation_id": invocation,
            "attempt_ids": [attempt],
            "resolution": "executed",
        }
    finally:
        engine.finish()
        machine.stop(drain=False)


def test_job_query_reads_authoritative_identity_and_detaches_attempts():
    instr = SimpleNamespace(addr=0, node="work", op="call", span=None, origin_dict=lambda: None)
    entry = SimpleNamespace(name="main")
    job = Job("same-source-key", entry, {})
    inflight = Inflight(job, instr, (0,))
    inflight.invocation_id = "1" * 64
    inflight.attempt_ids = ["2" * 32, "3" * 32]
    inflight.resolution = "executed"
    inflight.item = SimpleNamespace(
        ext={
            "job_execution_id": "forged",
            "invocation_id": "forged",
            "attempt_ids": ["forged"],
            "unit": "worker",
        }
    )
    job.stats["job_execution_id"] = "forged stats"
    job.window.add(inflight)
    machine = SimpleNamespace(
        dispatcher=SimpleNamespace(jobs={job.id: job}), code=SimpleNamespace(mem=[instr])
    )
    answer = q_job(machine, job.id)
    assert re.fullmatch(r"[0-9a-f]{32}", answer["job_execution_id"])
    assert answer["job_execution_id"] == job.job_execution_id
    assert q_jobs(machine)[0]["job_execution_id"] == job.job_execution_id
    row = answer["window"][0]
    assert row["invocation_id"] == "1" * 64
    assert row["attempt_ids"] == ["2" * 32, "3" * 32]
    assert row["resolution"] == "executed"
    row["attempt_ids"].append("4" * 32)
    assert len(inflight.attempt_ids) == 2


def test_old_native_view_missing_identity_reports_none_not_stats_fallback():
    old = SimpleNamespace(
        id=1,
        key="legacy",
        entry=SimpleNamespace(name="main"),
        strands={},
        window=[],
        unfinished=0,
        paused=False,
        inputs={},
        stats={"job_execution_id": "not authoritative"},
        slot_counts=lambda: (0, 0),
    )
    machine = SimpleNamespace(dispatcher=SimpleNamespace(jobs={1: old}))
    assert q_job(machine, 1)["job_execution_id"] is None
    assert q_jobs(machine)[0]["job_execution_id"] is None


@pytest.mark.parametrize(
    "resolution", ["executed", "content_hit", "positional_hit", "not_submitted", "squash_reuse"]
)
def test_archive_preserves_aligned_identity_sidecars_and_five_tuple(tmp_path, resolution):
    attempts = ["a" * 32, "b" * 32] if resolution == "executed" else []
    sidecar = []
    record_invocation(sidecar, 1, "c" * 64, attempts, resolution)
    assert sidecar[0] is None  # Unknown legacy prefix remains unknown, never fabricated.
    calls = [("neg", "digest", "ok", -1, None), ("neg", "digest", "ok", -2, None)]
    stats = {
        "identity_version": 1,
        "job_execution_id": "d" * 32,
        "record_invocations": sidecar,
        "policy_failures": [{"stage": "existing-sidecar"}],
    }
    record = JobRecord("job", "main", True, {"result": -2}, None, [], stats=stats, record=calls)
    writer = ArchiveWriter(tmp_path)
    try:
        writer.add_job(record, pickle.dumps({}), "program")
    finally:
        writer.close()
    [restored] = list(Archive(tmp_path).jobs())
    assert restored["record"] == calls and all(len(call) == 5 for call in restored["record"])
    assert restored["stats"] == stats
    assert len(restored["stats"]["record_invocations"]) == len(restored["record"])
    assert set(restored["stats"]["record_invocations"][1]) == {
        "invocation_id",
        "attempt_ids",
        "resolution",
    }


def test_reuse_declaration_report_separate_from_implementation_revision():
    library = NodeLibrary("report_identity")

    @library.node()
    def sample(x: int) -> int:
        return x

    for deterministic in (None, False, True):
        machine = Machine(
            program="(1)sample(result)\n",
            libraries=[library],
            passes=[],
            core="python",
            start_units=False,
            bind={"sample": Inline(implementation_revision="host/1", deterministic=deterministic)},
        )
        try:
            ctx = PluginContext(machine, BasePlugin())
            report = machine_spec(ctx)
            row = report["programs"][0]["calls"][0]
            assert row["reuse"] == {
                "deterministic": deterministic is True,
                "source": "none" if deterministic is None else "binding",
            }
            assert report["bindings"]["sample"]["deterministic"] is deterministic
            original = copy.deepcopy(ctx.query_now("programs")[0]["calls"][0]["reuse"])
            row["reuse"]["deterministic"] = "edited"
            assert ctx.query_now("programs")[0]["calls"][0]["reuse"] == original
        finally:
            machine.stop()


@pytest.mark.parametrize("store_mode", ["content", "positional"])
def test_report_determinism_does_not_certify_mutating_store_reuse(store_mode):
    library = NodeLibrary("report_mutation")

    @library.node(mutates=["items"])
    def change(items: list) -> list:
        items.append(1)
        return items

    machine = Machine(
        program="([])change(result)\n",
        libraries=[library],
        passes=[],
        core="python",
        start_units=False,
        store=MemoryStore(),
        bind={"change": Use(SimEngine("call"), store=store_mode, deterministic=True)},
    )
    try:
        report = machine_spec(PluginContext(machine, BasePlugin()))
        [call] = report["programs"][0]["execution"]["identity"]["calls"]
        assert call["deterministic"] is True
        assert call["reuse"] == {"content": False, "positional": False, "squash": False}
        assert report["bindings"]["change"]["store"] == store_mode  # Configured, not effective.
        assert any("mutating" in warning and "ignored" in warning for warning in report["warnings"])
        [instruction] = list(machine.code.calls(machine.code.regions[0]))
        assert instruction.binding.store is None
    finally:
        machine.stop()


@pytest.mark.parametrize("limit", [None, 1])
def test_real_record_limit_keeps_sidecars_aligned_or_drops_both(limit):
    machine = Machine(
        program="(1)neg(a)\n(a)neg(b)\n",
        bind={"neg": Inline()},
        core="python",
        record=1.0,
        record_limit=limit,
    )
    try:
        record = machine.submit({}, key="same-source-key").result(5)
    finally:
        machine.stop()
    assert re.fullmatch(r"[0-9a-f]{32}", record.stats["job_execution_id"])
    assert record.stats["identity_version"] == 1
    if limit is not None:
        assert record.record is None
        assert "record_invocations" not in record.stats
    else:
        assert len(record.record) == len(record.stats["record_invocations"]) == 2
        assert all(len(call) == 5 for call in record.record)
        for row in record.stats["record_invocations"]:
            assert re.fullmatch(r"[0-9a-f]{64}", row["invocation_id"])
            assert row["resolution"] == "executed"
            assert len(row["attempt_ids"]) == 1
