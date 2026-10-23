"""Semantic-event replay checks captured portable events, never consumer delivery."""

import copy
import io
import pickle
from types import SimpleNamespace

import pytest
from kohakunode import NodeLibrary
from kohakunode.events import Event

from togeari.cli.commands.conform import run as conform_command
from togeari.cli.replay import replay_dir
from togeari.conformance.replay import conform, replay
from togeari.core.construct.errors import Problems
from togeari.core.contracts.digest import digest
from togeari.core.contracts.events import CommittedEvent
from togeari.core.contracts.io import JobRecord
from togeari.core.program.job import build_job_programs
from togeari.plugins.record.archive import ArchiveWriter, program_info

SOURCE = '("updates", [1])emit()\n("updates", [1])emit()\nresult = 7\n'


def fixture_record(source=SOURCE):
    problems = Problems()
    [program] = build_job_programs(source, [None], [], None, [], problems)
    problems.raise_if_any()
    uid = "a" * 32
    invocations = ["b" * 64, "c" * 64]
    records = [("emit", digest({"topic": "updates", "payload": [1]}).hex(), "ok", None, None)] * 2
    envelopes = [
        CommittedEvent(
            uid,
            index,
            invocations[index],
            "updates",
            Event.capture("updates", [1]).to_dict()["payload"],
        ).to_dict()
        for index in range(2)
    ]
    job = JobRecord(
        "job",
        "main",
        True,
        {"result": 7},
        None,
        [],
        record=records,
        stats={
            "identity_version": 1,
            "job_execution_id": uid,
            "record_invocations": [
                {"invocation_id": value, "attempt_ids": [], "resolution": "executed"}
                for value in invocations
            ],
            "committed_events": {"version": 1, "events": envelopes},
        },
    )
    return program, job


def test_reference_replay_checks_events_and_preserves_existing_tuple_api():
    program, job = fixture_record()
    before = copy.deepcopy(job)
    verdict = conform(program, job, {})
    assert verdict.ok and verdict.event_status == "verified"
    assert replay(program, job.record, {}) == ("ok", {"result": 7}, [])
    assert job == before
    rows = job.stats["committed_events"]["events"]
    assert rows[0]["event_id"] != rows[1]["event_id"]


@pytest.mark.parametrize("stop", [0, 1])
def test_incomplete_replay_does_not_certify_matching_event_prefix(stop):
    program, job = fixture_record()
    job.record[stop] = ("wrong_node", *job.record[stop][1:])
    job.stats["committed_events"]["events"] = job.stats["committed_events"]["events"][:stop]
    verdict = conform(program, job, {})
    assert not verdict.ok and verdict.event_status == "unverified"
    assert any("stopped before completing" in detail for detail in verdict.details)


@pytest.mark.parametrize(
    "change",
    ["payload", "topic", "sequence", "swap", "drop", "duplicate", "invocation", "job", "version"],
)
def test_event_tampering_or_reordering_fails(change):
    program, job = fixture_record()
    rows = job.stats["committed_events"]["events"]
    if change == "payload":
        rows[0]["encoded_payload"] = Event.capture("updates", [True]).to_dict()["payload"]
    elif change == "topic":
        rows[0]["topic"] = "other"
    elif change == "sequence":
        rows[0]["sequence"] = 1
    elif change == "swap":
        rows.reverse()
    elif change == "drop":
        rows.pop()
    elif change == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif change == "invocation":
        job.stats["record_invocations"][0]["invocation_id"] = "d" * 64
    elif change == "job":
        job.stats["job_execution_id"] = "d" * 32
    elif change == "version":
        job.stats["committed_events"]["version"] = True
    verdict = conform(program, job, {})
    assert not verdict.ok and verdict.event_status == "mismatch"


@pytest.mark.parametrize("missing", ["committed_events", "record_invocations", "job_execution_id"])
def test_legacy_emitting_trace_is_explicitly_unverified(missing):
    program, job = fixture_record()
    job.stats.pop(missing)
    verdict = conform(program, job, {})
    assert not verdict.ok and verdict.event_status == "unverified"
    assert "unverified" in verdict.reason


def test_nonemitting_legacy_trace_remains_applicable_without_event_sidecars():
    problems = Problems()
    [program] = build_job_programs("result = 7\n", [None], [], None, [], problems)
    record = JobRecord("job", "main", True, {"result": 7}, None, [], record=[])
    verdict = conform(program, record, {})
    assert verdict.ok and verdict.event_status == "not_applicable"


def test_committed_prefix_is_checked_when_later_call_fails():
    program, record = fixture_record(SOURCE.replace("result = 7", "(1, 0)div(result)"))
    error = {"kind": "ZeroDivisionError", "message": "division by zero", "node": "div"}
    record.record.append(("div", digest({"a": 1, "b": 0}).hex(), "error", error, None))
    record.ok = False
    record.outputs = {}
    record.failure = {"kind": "ZeroDivisionError", "node": "div"}
    verdict = conform(program, record, {})
    assert verdict.ok and verdict.event_status == "verified"
    record.stats["committed_events"]["events"].pop()
    assert not conform(program, record, {}).ok


def test_authentic_alias_emits_but_host_named_emit_is_ordinary():
    program, job = fixture_record('@use "kir.std" as s\n' + SOURCE.replace(")emit(", ")s.emit("))
    job.record = [("s.emit", *record[1:]) for record in job.record]
    assert conform(program, job, {}).event_status == "verified"
    host = NodeLibrary("host_events")

    @host.node(outputs=[])
    def emit(topic: str, payload):
        return None

    problems = Problems()
    [program] = build_job_programs(SOURCE, [None], [host], None, [], problems)
    problems.raise_if_any()
    _, record = fixture_record()
    record.stats = {}
    verdict = conform(program, record, {})
    assert verdict.ok and verdict.event_status == "not_applicable"


def test_event_origins_are_explanatory_not_semantic_equality():
    program, job = fixture_record()
    for row in job.stats["committed_events"]["events"]:
        row["origin"] = {"module": "moved.kir", "line": 999}
    assert conform(program, job, {}).event_status == "verified"


def test_invalid_emit_error_visible_without_committed_event():
    problems = Problems()
    [program] = build_job_programs(
        '@def (value)main():\n    ("bad", value)emit()\n', ["main"], [], None, [], problems
    )
    problems.raise_if_any()
    payload = object()
    err = {"kind": "TypeError", "message": "unsupported payload", "node": "emit"}
    job = JobRecord(
        "job",
        "main",
        False,
        {},
        {"kind": "TypeError", "node": "emit"},
        [],
        record=[("emit", digest({"topic": "bad", "payload": payload}).hex(), "error", err, None)],
        stats={"committed_events": {"version": 1, "events": []}},
    )
    result = conform(program, job, {"value": payload})
    assert result.ok and result.event_status == "verified"
    job.record[0] = (*job.record[0][:2], "ok", None, None)
    job.ok = True
    job.outputs = {}
    assert not conform(program, job, {"value": payload}).ok


def test_archive_cli_forwards_event_stats_and_prints_unverified(tmp_path):
    program, job = fixture_record()
    writer = ArchiveWriter(tmp_path)
    try:
        writer.add_program(program_info(program, [], None))
        writer.add_job(job, pickle.dumps({}), program.semantic_hash)
    finally:
        writer.close()
    [result] = list(replay_dir(tmp_path))
    assert result.verdict.ok and result.verdict.event_status == "verified"
    job.stats.pop("committed_events")
    writer = ArchiveWriter(tmp_path)
    try:
        writer.add_program(program_info(program, [], None))
        writer.add_job(job, pickle.dumps({}), program.semantic_hash)
    finally:
        writer.close()
    args = SimpleNamespace(
        dir=tmp_path,
        lib=[],
        run=[writer.run],
        reexecute=[],
        step_budget=1000,
        engine_errors=True,
        max_fail=10,
    )
    output = io.StringIO()
    assert conform_command(args, output) == 1
    assert "semantic events: 1 unverified" in output.getvalue()
