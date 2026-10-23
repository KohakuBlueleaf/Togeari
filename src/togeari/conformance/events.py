"""Compare semantic event snapshots without opening a journal or contacting a sink."""

from kohakunode.events import Event
from kohakunode.sig.env import Resolved
from kohakunode.sig.std import is_standard_emit
from kohakunode.syntax.ast import Call, walk

from togeari.core.contracts.events import CommittedEvent


def emits(job_program) -> bool:
    """Identify maintained standard emit by contract/implementation, never spelling alone."""
    for stmt in walk(job_program.written.body):
        if isinstance(stmt, Call):
            resolved = job_program.env.resolve(stmt.name)
            if isinstance(resolved, Resolved) and is_standard_emit(
                resolved.library, resolved.signature, job_program.env.impl(stmt.name)
            ):
                return True
    return False


def compare_events(job_program, job_record, captured) -> tuple[str, str]:
    """Return event verification status and explanation; five-field records stay unchanged."""
    stats = getattr(job_record, "stats", {}) or {}
    recorded = stats.get("committed_events")
    if recorded is None:
        if captured or emits(job_program):
            return (
                "unverified",
                "semantic events unverified: legacy record has no committed-event sidecar",
            )
        return "not_applicable", ""
    if (
        type(recorded) is not dict
        or type(recorded.get("version")) is not int
        or recorded["version"] != 1
    ):
        return "mismatch", "invalid committed-event sidecar version"
    rows = recorded.get("events")
    if type(rows) is not list:
        return "mismatch", "invalid committed-event sidecar events"
    if len(rows) != len(captured):
        return "mismatch", "committed event counts differ"
    invocations = stats.get("record_invocations")
    jobuid = stats.get("job_execution_id")
    unverifiable = False
    seen = set()
    for sequence, (row, (event, call_index)) in enumerate(zip(rows, captured)):
        try:
            envelope = CommittedEvent.from_dict(row)
            semantic = Event(envelope.topic, envelope.encoded_payload)
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            return "mismatch", f"invalid committed event {sequence}: {type(exc).__name__}"
        if envelope.event_id in seen:
            return "mismatch", f"committed event {sequence}: duplicate event identity"
        seen.add(envelope.event_id)
        if envelope.sequence != sequence:
            return "mismatch", f"committed event {sequence}: sequence differs"
        if event != semantic:
            return "mismatch", f"committed event {sequence}: topic or payload differs"
        if jobuid is None:
            unverifiable = True
        elif envelope.job_execution_id != jobuid:
            return "mismatch", f"committed event {sequence}: logical job identity differs"
        identity = (
            invocations[call_index]
            if type(invocations) is list and 0 <= call_index < len(invocations)
            else None
        )
        if identity is None:
            unverifiable = True
        elif type(identity) is not dict or envelope.invocation_id != identity.get("invocation_id"):
            return "mismatch", f"committed event {sequence}: associated invocation differs"
    if unverifiable:
        return (
            "unverified",
            "semantic event content matches, but invocation/job association is unverified",
        )
    return "verified", ""
