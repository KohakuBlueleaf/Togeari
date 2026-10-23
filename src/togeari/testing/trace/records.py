"""Comparable records: ignore wall time and alpha-rename runtime identity tokens.

Only known identity statistics are renamed. User outputs, failures, call payloads,
execution hashes and arbitrary ID-looking strings retain their original meaning.
"""

from dataclasses import dataclass, field
from typing import Any

from togeari.core.contracts.digest import digest
from togeari.core.contracts.io import JobRecord

TIMED_STATS = ("seconds",)


@dataclass
class IdentityContext:
    """One bijective first-occurrence renaming per identity domain for a whole trace."""

    jobs: dict[str, int] = field(default_factory=dict)
    invocations: dict[str, int] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)

    def token(self, value: Any, domain: str) -> Any:
        length = 64 if domain == "invocation" else 32
        # Do not let normalization conceal malformed identity values.
        if (
            type(value) is not str
            or len(value) != length
            or any(c not in "0123456789abcdef" for c in value)
        ):
            return value
        table = {
            "job": self.jobs,
            "invocation": self.invocations,
            "attempt": self.attempts,
        }[domain]
        if value not in table:
            table[value] = len(table)
        return domain, table[value]

    def stats(self, original: dict) -> dict:
        stats = {k: v for k, v in original.items() if k not in TIMED_STATS}
        if "job_execution_id" in stats:
            stats["job_execution_id"] = self.token(stats["job_execution_id"], "job")
        records = stats.get("record_invocations")
        if type(records) is list:
            normalized = []
            for record in records:
                if type(record) is not dict:
                    normalized.append(record)
                    continue
                record = dict(record)
                if "invocation_id" in record:
                    record["invocation_id"] = self.token(record["invocation_id"], "invocation")
                if type(record.get("attempt_ids")) is list:
                    record["attempt_ids"] = [
                        self.token(attempt, "attempt") for attempt in record["attempt_ids"]
                    ]
                normalized.append(record)
            stats["record_invocations"] = normalized
        return stats


def canonical(rec: JobRecord, identities: IdentityContext | None = None) -> tuple:
    """Canonical record; pass a shared context to retain identity relations across jobs."""
    identities = IdentityContext() if identities is None else identities
    return (
        rec.key,
        rec.entry,
        rec.ok,
        digest(rec.outputs).hex(),
        rec.failure,
        list(rec.missing),
        digest(identities.stats(rec.stats)).hex(),
        digest(rec.record).hex() if rec.record is not None else None,
    )


def records_digest(records: list[JobRecord]) -> str:
    """Digest ordered records, preserving cross-record token equality and multiplicity."""
    identities = IdentityContext()
    return digest([canonical(record, identities) for record in records]).hex()


def first_difference(a: list[JobRecord], b: list[JobRecord]) -> Any:
    """First differing canonical pair, comparing token relationships rather than UUID bytes."""
    left, right = IdentityContext(), IdentityContext()
    for i in range(max(len(a), len(b))):
        x = canonical(a[i], left) if i < len(a) else None
        y = canonical(b[i], right) if i < len(b) else None
        if x != y:
            return i, x, y
    return None
