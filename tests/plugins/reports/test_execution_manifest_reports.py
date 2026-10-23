"""Execution metadata is distinct from source hashes and outcome replay."""

import copy
import json
import pickle
import warnings

import pytest

from togeari import Inline, Lane, Machine, Use
from togeari.cli.replay import replay_dir
from togeari.core.construct.errors import Problems
from togeari.core.contracts.digest import digest
from togeari.core.contracts.io import JobRecord
from togeari.core.plugin.base import BasePlugin, PluginContext
from togeari.core.plugin.tables import c_set_knob
from togeari.core.program.execution import current_execution, execution_context
from togeari.core.program.job import build_job_programs
from togeari.plugins.record.archive import Archive, ArchiveWriter, program_info
from togeari.plugins.record.recorder import Recorder
from togeari.plugins.report.spec import machine_spec
from togeari.testing.sim import SimEngine


def report_machine(*, revision=None, lane=False):
    binding = (
        Use(Lane([SimEngine("call", name="sim")], name="work"), binding_revision=revision)
        if lane
        else Inline(implementation_revision=revision)
    )
    return Machine(
        program="(1)neg(result)\n",
        passes=[],
        core="python",
        start_units=False,
        bind={"neg": binding},
    )


@pytest.mark.parametrize("attested", [False, True])
def test_report_manifest_is_current_detached_and_separate_from_source(attested):
    machine = report_machine(revision="bundle/1" if attested else None, lane=True)
    try:
        ctx = PluginContext(machine, BasePlugin())
        region = machine.code.regions[0]
        initial = copy.deepcopy(region.execution)
        expected = copy.deepcopy(current_execution(region, machine.code, machine))
        queried = ctx.query_now("programs")[0]
        reported = machine_spec(ctx)["programs"][0]
        assert queried["execution"] == reported["execution"] == expected
        manifest = reported["execution"]
        assert manifest["schema"] == "togeari.execution" and manifest["version"] == 1
        assert manifest["completeness"]["complete"] is attested
        assert bool(manifest["completeness"]["unknown"]) is not attested
        assert manifest["identity"]["program"]["semantic_hash"] == reported["semantic_hash"]
        assert manifest["hash"] != reported["semantic_hash"]
        assert "origin" not in manifest and queried["origins"]
        json.dumps(reported, allow_nan=False)
        queried["execution"]["hash"] = "edited query hash"
        queried["execution"]["identity"]["calls"][0]["policy"]["timeout_s"] = 999
        assert ctx.query_now("programs")[0]["execution"] == expected
        assert reported["execution"] == expected
        original_hash = manifest["hash"]
        machine.lanes[0].name = "renamed placement"
        machine.lanes[0].max_inflight = 12
        unchanged = machine_spec(ctx)["programs"][0]
        assert unchanged["execution"]["hash"] == original_hash
        assert unchanged["calls"][0]["lane"] == "renamed placement"
        machine.lanes[0].timeout_s = 0.25
        changed = machine_spec(ctx)["programs"][0]
        assert changed["semantic_hash"] == reported["semantic_hash"]
        assert changed["execution"]["hash"] != original_hash
        assert region.execution == initial
        changed["execution"]["completeness"]["unknown"].append({"path": "bad", "reason": "edit"})
        assert {"path": "bad", "reason": "edit"} not in current_execution(
            region, machine.code, machine
        )["completeness"]["unknown"]
    finally:
        machine.stop()


@pytest.mark.parametrize("program", ["(1)neg(result)\n", "result = 1\n"])
def test_step_budget_command_records_change_and_revert_for_active_context(program):
    machine = Machine(
        program=program,
        passes=[],
        core="python",
        start_units=False,
        bind={"neg": Inline()},
        step_budget=1000,
    )
    try:
        region = machine.code.regions[0]
        admitted = copy.deepcopy(current_execution(region, machine.code, machine))
        assert admitted["completeness"]["complete"]
        assert c_set_knob(machine, "step_budget", 500) == 1000
        assert c_set_knob(machine, "step_budget", 1000) == 500
        active = execution_context(region, machine.code, machine, admitted["epoch"])
        assert active["hash"] == admitted["hash"]
        assert not active["completeness"]["complete"]
        assert len(active["completeness"]["transitions"]) == 2
        assert {change["kind"] for change in active["completeness"]["transitions"]} == {
            "policy_changed"
        }
        fresh = copy.deepcopy(current_execution(region, machine.code, machine))
        assert fresh["completeness"]["complete"]
        assert fresh["completeness"]["transitions"] == []
        epoch = fresh["epoch"]
        c_set_knob(machine, "step_budget", 1000)  # No-op is not a transition.
        c_set_knob(machine, "record", 0.5)  # Not an identity knob in this version.
        assert current_execution(region, machine.code, machine)["epoch"] == epoch
    finally:
        machine.stop()


def test_archive_keeps_initial_region_identity_and_exact_job_stats_per_run(tmp_path):
    expected = {}
    semantic = None
    for revision in ("implementation/1", "implementation/2"):
        machine = report_machine(revision=revision)
        recorder = Recorder(tmp_path)
        try:
            region = machine.code.regions[0]
            initial = copy.deepcopy(region.execution)
            info = program_info(region.job, [], None, execution=initial)
            assert info["schema_version"] == 2 and info["hash"] == region.semantic_hash
            assert info["execution_at"] == "region_binding"
            info["execution"]["hash"] = "mutation"
            assert initial["hash"] != "mutation"
            admission_epoch = current_execution(region, machine.code, machine)["epoch"]
            machine.knobs["step_budget"] = 1000
            current = execution_context(region, machine.code, machine, admission_epoch)
            assert current["hash"] != initial["hash"]
            assert current["completeness"]["transitions"]
            assert not current["completeness"]["complete"]
            recorder.on_load(PluginContext(machine, recorder))
            run = recorder.writer.run
            recorder.on_job_start({"key": revision, "recorded": True, "inputs": {}})
            stats = {
                "program": region.semantic_hash,
                "program_region": region.index,
                "execution": current,
                "execution_epoch": admission_epoch,
                "policy_failures": [{"kind": "preserved-sidecar"}],
            }
            record = JobRecord(
                revision,
                "main",
                True,
                {"result": -1},
                None,
                [],
                stats=stats,
                record=[("neg", digest({"a": 1}).hex(), "ok", -1, None)],
            )
            recorder.on_job_end(record)
            expected[run] = (initial, copy.deepcopy(stats))
            assert semantic is None or semantic == region.semantic_hash
            semantic = region.semantic_hash
        finally:
            recorder.on_unload()
            machine.stop()
    archive = Archive(tmp_path)
    assert len(archive.programs) == 1 and len(archive.runs()) == 2
    for job in archive.jobs():
        initial, stats = expected[job["run"]]
        artifact = archive.program(semantic, job["run"], job["stats"]["program_region"])
        assert artifact["execution"] == initial
        assert artifact["execution_at"] == "region_binding"
        assert job["stats"] == stats
        assert job["stats"]["execution"]["hash"] != artifact["execution"]["hash"]
    assert len({item[0]["hash"] for item in expected.values()}) == 2
    assert all(result.verdict.ok for result in replay_dir(tmp_path))


def legacy_archive(path, *, execution=None, regions=(0,)):
    problems = Problems()
    [job] = build_job_programs("(1)neg(result)\n", [None], [], None, [], problems)
    problems.raise_if_any()
    writer = ArchiveWriter(path)
    try:
        for region in regions:
            info = program_info(job, [], None)
            info["region"] = region
            if execution is not None:
                info["execution"] = execution
                info["execution_at"] = "region_binding"
            writer.add_program(info)
            for index in range(2):
                record = JobRecord(
                    f"{region}-{index}",
                    "main",
                    True,
                    {"result": -1},
                    None,
                    [],
                    stats={"program": job.semantic_hash, "program_region": region},
                    record=[("neg", digest({"a": 1}).hex(), "ok", -1, None)],
                )
                writer.add_job(record, pickle.dumps({}), job.semantic_hash)
    finally:
        writer.close()
    return writer.run, job.semantic_hash


@pytest.mark.parametrize(
    "execution", [None, {"hash": "untrusted", "complete": False, "unknown": ["host"]}]
)
def test_ordinary_replay_accepts_legacy_and_unknown_execution_metadata(tmp_path, execution):
    run, program = legacy_archive(tmp_path, execution=execution)
    with warnings.catch_warnings(record=True) as found:
        warnings.simplefilter("always")
        replayed = list(replay_dir(tmp_path))
    assert len(replayed) == 2 and all(item.verdict.ok for item in replayed)
    assert not [warning for warning in found if "compatibility" in str(warning.message)]
    artifact = Archive(tmp_path).program(program, run, 0)
    assert artifact.get("execution") == execution


def test_selected_reexecution_warns_once_per_region_not_per_job(tmp_path):
    legacy_archive(tmp_path, regions=(0, 1))
    with pytest.warns(RuntimeWarning, match="compatibility is unverified") as found:
        replayed = list(replay_dir(tmp_path, reexecute=["neg"]))
    assert len(replayed) == 4 and all(item.verdict.ok for item in replayed)
    assert len(found) == 2
    assert "region 0" in str(found[0].message)
    assert "region 1" in str(found[1].message)
    assert all("do not certify execution identity" in str(w.message) for w in found)
