"""Logical identity helpers are portable and never infer invocation from input equality."""

import hashlib
import json
import uuid

import pytest

from togeari.core.contracts.identity import (
    invocation_id,
    new_attempt_id,
    new_job_execution_id,
    validate_attempt_id,
    validate_invocation_id,
    validate_job_execution_id,
)

JOB = "0123456789abcdef" * 2
PROGRAM = "a" * 64
EXECUTION = "b" * 64


def test_job_and_attempt_tokens_are_fresh_canonical_uuid4():
    jobs = [new_job_execution_id() for _ in range(50)]
    attempts = [new_attempt_id() for _ in range(50)]
    assert len(set(jobs + attempts)) == 100
    for value in jobs:
        assert validate_job_execution_id(value) == value
        assert uuid.UUID(hex=value).version == 4
    for value in attempts:
        assert validate_attempt_id(value) == value
        assert uuid.UUID(hex=value).version == 4


def test_invocation_has_exact_versioned_canonical_encoding():
    args = (JOB, PROGRAM, EXECUTION, 7, (0, 2, 1))
    payload = ["togeari.invocation", 1, *args]
    expected = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()
    assert invocation_id(*args) == expected
    assert validate_invocation_id(expected) == expected
    assert invocation_id(*args) == invocation_id(*args)  # Retry/recovery of this call.


@pytest.mark.parametrize("part", range(5))
def test_every_logical_coordinate_changes_identity(part):
    original = [JOB, PROGRAM, EXECUTION, 7, (0, 2, 1)]
    changed = list(original)
    changed[part] = ["f" * 32, "c" * 64, "d" * 64, 8, (0, 2, 2)][part]
    assert invocation_id(*original) != invocation_id(*changed)


def test_no_inputs_origin_or_attempt_fields_enter_invocation_identity():
    args = (JOB, PROGRAM, EXECUTION, 7, (0, 2, 1))
    for kw in ({"inputs": {}}, {"origin": "module:1"}, {"attempt_id": new_attempt_id()}):
        with pytest.raises(TypeError):
            invocation_id(*args, **kw)
    assert invocation_id(JOB, PROGRAM, EXECUTION, 7, ())


@pytest.mark.parametrize("validate", [validate_job_execution_id, validate_attempt_id])
@pytest.mark.parametrize(
    "bad", [None, 1, b"a" * 32, "a" * 31, "a" * 33, "A" * 32, "g" * 32, "a" * 32 + "\n"]
)
def test_uuid_validation_does_not_normalize_invalid_tokens(validate, bad):
    with pytest.raises(ValueError):
        validate(bad)


@pytest.mark.parametrize("bad", [None, "a" * 32, "a" * 63, "a" * 65, "A" * 64, "g" * 64])
def test_invocation_validation_requires_full_lowercase_sha256(bad):
    with pytest.raises(ValueError):
        validate_invocation_id(bad)


@pytest.mark.parametrize("bad", [True, -1, 1 << 63, 0.0, "0", None])
def test_invalid_callsite_rejected(bad):
    with pytest.raises(ValueError, match="callsite"):
        invocation_id(JOB, PROGRAM, EXECUTION, bad, (0,))


@pytest.mark.parametrize(
    "bad", [[0], (True,), (-1,), (1 << 63,), (0.0,), (float("inf"),), ("0",), None]
)
def test_invalid_logical_order_rejected(bad):
    with pytest.raises(ValueError, match="order"):
        invocation_id(JOB, PROGRAM, EXECUTION, 0, bad)


@pytest.mark.parametrize("field", ["job_execution_id", "program_hash", "execution_hash"])
def test_invalid_identity_component_rejected(field):
    args = dict(
        job_execution_id=JOB, program_hash=PROGRAM, execution_hash=EXECUTION, callsite=0, order=(0,)
    )
    args[field] = "invalid"
    with pytest.raises(ValueError, match=field):
        invocation_id(**args)
