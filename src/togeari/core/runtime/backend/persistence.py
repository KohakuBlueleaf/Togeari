"""Execution-compatible result reuse shared by both dispatcher implementations."""

import copy
import logging

from togeari.core.program.execution import current_execution, reuse_decision, warn_unverified

log = logging.getLogger("togeari.persistence")


def _denied(status, code, path, message):
    return {
        "status": status,
        "allow": False,
        "issues": [{"code": code, "path": path, "message": message}],
    }


def result_execution(region, code, machine, stats: dict) -> str | None:
    """Check trusted admission identity/history, then cached current-manifest completeness.

    External snapshot candidates use full compatibility validation elsewhere. An incomplete
    strict result identity is a diagnosed cache miss, not a job failure. Recheck at lookup
    and write boundaries because user callbacks can change the execution configuration.
    """
    current = current_execution(region, code, machine)
    admission = stats.get("execution")
    admission_hash = admission.get("hash") if isinstance(admission, dict) else None
    epoch = stats.get("execution_epoch")
    mode = machine.persistence_compatibility
    valid_admission = admission_hash is not None and type(epoch) is int
    mixed = bool(
        epoch != current["epoch"]
        or (isinstance(admission, dict) and admission.get("completeness", {}).get("transitions"))
        or current["completeness"]["transitions"]
    )
    same_identity = admission_hash == current["hash"]
    context = (current["hash"], current["epoch"], mode, admission_hash, epoch, mixed)
    cached = stats.get("persistence")
    if (
        stats.get("_persistence_context") == context
        and isinstance(cached, dict)
        and type(cached.get("allow")) is bool
        and (not cached["allow"] or (valid_admission and not mixed and same_identity))
    ):
        return current["hash"] if cached["allow"] else None

    # Local epochs are not portable identity, but a change-and-revert still breaks history.
    if not valid_admission:
        decision = _denied(
            "invalid", "invalid_manifest", "admission", "missing execution admission"
        )
    elif mixed:
        decision = _denied(
            "incompatible",
            "unsupported_history",
            "completeness.transitions",
            "mixed execution configuration history is not reusable",
        )
    elif not same_identity:
        decision = _denied(
            "incompatible",
            "identity_mismatch",
            "identity",
            "execution, signature, policy or code layout differs",
        )
    else:
        decision = reuse_decision(region, code, machine, mode)

    # Cache only primitive context and a detached decision, never a borrowed manifest object.
    stats["persistence"] = copy.deepcopy(decision)
    stats["_persistence_context"] = context
    if decision["allow"]:
        warn_unverified(machine, decision, execution_hash=current["hash"])
        return current["hash"]
    for issue in decision["issues"]:
        key = ("result", current["hash"], issue["code"], issue["path"], issue["message"])
        if key not in machine._persistence_warnings:
            machine._persistence_warnings.add(key)
            log.warning("result persistence skipped: %s: %s", issue["path"], issue["message"])
    return None
