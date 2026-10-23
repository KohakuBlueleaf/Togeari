"""Invocation-record sidecars shared by both cores; no scheduler/state dependencies."""

from togeari.core.contracts.identity import validate_attempt_id, validate_invocation_id

RESOLUTIONS = frozenset(
    {"executed", "content_hit", "positional_hit", "not_submitted", "squash_reuse"}
)


def record_invocation(
    records: list,
    index: int,
    invocation_id: str,
    attempt_ids: list[str],
    resolution: str,
) -> None:
    """Append alongside an unchanged call tuple; pad an old snapshot's unknown prefix."""
    validate_invocation_id(invocation_id)
    attempts = [validate_attempt_id(value) for value in attempt_ids]
    if resolution not in RESOLUTIONS:
        raise ValueError(f"unknown invocation resolution {resolution!r}")
    if type(index) is not int or index < len(records):
        raise ValueError("invocation record index must append after its existing prefix")
    records.extend([None] * (index - len(records)))
    records.append(
        {
            "invocation_id": invocation_id,
            "attempt_ids": attempts,
            "resolution": resolution,
        }
    )
