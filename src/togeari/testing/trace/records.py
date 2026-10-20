"""Records in a form two runs can compare: every field but wall-clock times, as a digest."""

from typing import Any

from togeari.core.contracts.digest import digest
from togeari.core.contracts.io import JobRecord

# Stats measured in wall-clock time; the rest of a record is decided by the machine
TIMED_STATS = ("seconds",)


def canonical(rec: JobRecord) -> tuple:
    """The record's fields, outputs and conformance record as digests, stats without times."""
    stats = {k: v for k, v in rec.stats.items() if k not in TIMED_STATS}
    return (
        rec.key,
        rec.entry,
        rec.ok,
        digest(rec.outputs).hex(),
        rec.failure,
        list(rec.missing),
        digest(stats).hex(),
        digest(rec.record).hex() if rec.record is not None else None,
    )


def records_digest(records: list[JobRecord]) -> str:
    """One digest of every record in the order given."""
    return digest([canonical(r) for r in records]).hex()


def first_difference(a: list[JobRecord], b: list[JobRecord]) -> Any:
    """The first ``(index, mine, other)`` canonical pair that differs, or None."""
    for i in range(max(len(a), len(b))):
        x = canonical(a[i]) if i < len(a) else None
        y = canonical(b[i]) if i < len(b) else None
        if x != y:
            return i, x, y
    return None
