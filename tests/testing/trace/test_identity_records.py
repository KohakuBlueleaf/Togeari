"""Trace identity normalization preserves relationships rather than deleting metadata."""

import copy

import pytest

from togeari.core.contracts.io import JobRecord
from togeari.testing.trace.records import (
    IdentityContext,
    canonical,
    first_difference,
    records_digest,
)


def record(job, invocations, attempts, *, key="job", resolutions=None):
    resolutions = resolutions or ["executed"] * len(invocations)
    return JobRecord(
        key,
        "main",
        True,
        {"value": 7},
        stats={
            "seconds": 1.0,
            "identity_version": 1,
            "job_execution_id": job,
            "record_invocations": [
                {"invocation_id": invocation, "attempt_ids": list(actual), "resolution": resolution}
                for invocation, actual, resolution in zip(invocations, attempts, resolutions)
            ],
        },
        record=[("op", "digest", "ok", 7, None) for _ in invocations],
    )


def test_independent_uuid_bytes_preserve_equivalent_trace_identity():
    left = [record("a" * 32, ["b" * 64, "c" * 64], [["d" * 32], ["e" * 32, "f" * 32]])]
    right = [record("1" * 32, ["2" * 64, "3" * 64], [["4" * 32], ["5" * 32, "6" * 32]])]
    before = copy.deepcopy(left)
    assert first_difference(left, right) is None
    assert records_digest(left) == records_digest(right)
    assert canonical(left[0]) == canonical(right[0])
    assert left == before


@pytest.mark.parametrize("kind", ["job", "invocation", "attempt"])
def test_cross_job_duplicate_identity_is_not_hidden(kind):
    left = [
        record("1" * 32, ["2" * 64], [["3" * 32]], key="first"),
        record("4" * 32, ["5" * 64], [["6" * 32]], key="second"),
    ]
    right = copy.deepcopy(left)
    match kind:
        case "job":
            right[1].stats["job_execution_id"] = right[0].stats["job_execution_id"]
        case "invocation":
            right[1].stats["record_invocations"][0]["invocation_id"] = "2" * 64
        case "attempt":
            right[1].stats["record_invocations"][0]["attempt_ids"] = ["3" * 32]
    assert first_difference(left, right)[0] == 1
    assert records_digest(left) != records_digest(right)


@pytest.mark.parametrize("kind", ["invocation", "attempt", "count", "resolution", "missing"])
def test_within_job_relations_and_sidecar_shape_remain_observable(kind):
    left = record("1" * 32, ["2" * 64, "3" * 64], [["4" * 32], ["5" * 32]])
    right = copy.deepcopy(left)
    entries = right.stats["record_invocations"]
    match kind:
        case "invocation":
            entries[1]["invocation_id"] = entries[0]["invocation_id"]
        case "attempt":
            entries[1]["attempt_ids"] = entries[0]["attempt_ids"]
        case "count":
            entries[1]["attempt_ids"].append("6" * 32)
        case "resolution":
            entries[1]["resolution"] = "positional_hit"
        case "missing":
            entries[1] = None
    assert first_difference([left], [right]) is not None


def test_resumed_prefix_equality_and_attempt_order_survive_whole_trace_mapping():
    left = [
        record("1" * 32, ["2" * 64], [["3" * 32, "4" * 32]], key="checkpoint"),
        record("1" * 32, ["2" * 64], [["3" * 32, "4" * 32]], key="resumed"),
    ]
    right = [
        record("a" * 32, ["b" * 64], [["c" * 32, "d" * 32]], key="checkpoint"),
        record("a" * 32, ["b" * 64], [["c" * 32, "d" * 32]], key="resumed"),
    ]
    assert first_difference(left, right) is None
    right[1].stats["record_invocations"][0]["attempt_ids"].reverse()
    assert first_difference(left, right)[0] == 1


def test_optional_context_keeps_standalone_api_and_cross_record_relations():
    context = IdentityContext()
    first = record("1" * 32, ["2" * 64], [["3" * 32]])
    second = record("4" * 32, ["5" * 64], [["6" * 32]])
    assert canonical(first) == canonical(second)
    assert canonical(first, context) != canonical(second, context)


@pytest.mark.parametrize("location", ["outputs", "failure", "arbitrary_stats", "malformed"])
def test_unrelated_user_values_and_invalid_tokens_are_not_normalized(location):
    left = record("1" * 32, ["2" * 64], [["3" * 32]])
    right = copy.deepcopy(left)
    match location:
        case "outputs":
            right.outputs["invocation_id"] = "f" * 64
        case "failure":
            right.failure = {"invocation_id": "f" * 64}
        case "arbitrary_stats":
            right.stats["custom"] = {"invocation_id": "f" * 64}
        case "malformed":
            right.stats["job_execution_id"] = "bad-token"
    assert first_difference([left], [right]) is not None
