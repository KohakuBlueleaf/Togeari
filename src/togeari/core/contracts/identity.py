"""Durable logical-run and call identities, distinct from scheduling and source metadata.

UUIDs identify fresh jobs/attempts; a versioned SHA-256 identifies one dynamic invocation.
These tokens do not guarantee deterministic sampling or exactly-once external effects.
"""

import hashlib
import json
import re
import uuid

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_I64 = (1 << 63) - 1


def _hex(value: str, pattern: re.Pattern, name: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical lowercase hexadecimal string")
    return value


def validate_job_execution_id(value: str) -> str:
    """Validate a persisted 32-hex logical-run token without changing it."""
    return _hex(value, _HEX32, "job_execution_id")


def validate_attempt_id(value: str) -> str:
    """Validate a 32-hex actual-execution attempt token without changing it."""
    return _hex(value, _HEX32, "attempt_id")


def validate_invocation_id(value: str) -> str:
    """Validate a versioned invocation's full 64-hex SHA-256 identity."""
    return _hex(value, _HEX64, "invocation_id")


def new_job_execution_id() -> str:
    """Fresh logical run; source keys and equal inputs never determine this token."""
    return uuid.uuid4().hex


def new_attempt_id() -> str:
    """Fresh actual inline execution or per-item engine submission, including retries."""
    return uuid.uuid4().hex


def invocation_id(
    job_execution_id: str,
    program_hash: str,
    execution_hash: str,
    callsite: int,
    order: tuple[int, ...],
) -> str:
    """Identify one logical call under a compatible program/layout and execution contract.

    ``callsite`` is its region-relative instruction offset; ``order`` is its logical
    control/iteration position. Retrying the same call keeps these fields. A new logical
    occurrence or run changes them. Input equality and source/origin metadata play no role.
    """
    validate_job_execution_id(job_execution_id)
    _hex(program_hash, _HEX64, "program_hash")
    _hex(execution_hash, _HEX64, "execution_hash")
    if type(callsite) is not int or not 0 <= callsite <= _MAX_I64:
        raise ValueError("callsite must be a nonnegative signed-i64 integer")
    if type(order) is not tuple or any(
        type(part) is not int or not 0 <= part <= _MAX_I64 for part in order
    ):
        raise ValueError("order must be a tuple of nonnegative exact signed-i64 integers")
    payload = [
        "togeari.invocation",
        1,
        job_execution_id,
        program_hash,
        execution_hash,
        callsite,
        order,
    ]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
