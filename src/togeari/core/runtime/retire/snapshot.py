"""Job snapshots at retire boundaries, and resume from them (spec/03 §3.12).

A job snapshots when its window is empty and one strand is left: its state is
then only an address, an order key, frames and filled values (plus its
conformance record when it is recorded). Frames restore without their joins:
the restored strand runs past each reconvergence point itself. Snapshots go to the
machine's store under the program, the job key and the job's inputs.

Trust: a snapshot is a pickle, and ``restore`` unpickles what the store returns.
Unpickling runs code named in the data, so a store that holds snapshots must be
one only trusted writers can change.
"""

import logging
from collections.abc import Callable
from typing import Any

from togeari.core.contracts.digest import digest
from togeari.core.contracts.io import MISS
from togeari.core.runtime.retire.codec import VERSION, denied, encode_snapshot, prepare_restore
from togeari.core.runtime.state import Frame, NameMap, SlotState, Strand, const, effective

log = logging.getLogger("togeari.snapshot")
_FINAL = (float("inf"),)


def snapshot_key(region_hash: str, job_key: str, inputs_digest: bytes) -> bytes:
    return digest(("togeari.snapshot", VERSION, region_hash, job_key, inputs_digest))


def legacy_snapshot_key(region_hash: str, job_key: str, inputs_digest: bytes) -> bytes:
    """The v1 locator is probed only to diagnose migration, never to apply its state."""
    return digest(("snapshot", region_hash, job_key, inputs_digest))


def capture(job: Any, code: Any = None, manifest: dict | None = None) -> bytes | None:
    """The job's state as bytes, or None off a boundary; raises when a value cannot pickle."""
    if getattr(job, "collectors", None):
        return None
    live = list(job.strands.values())
    if job.window or len(live) != 1 or live[0].spawn is not None:
        return None
    if code is None:
        raise ValueError("snapshot v2 capture requires the actual CodeMemory")
    if manifest is None:
        manifest = job.entry.region.execution
    st = live[0]
    pc, prefix, counter, frames_now = st.pc, st.prefix, st.counter, list(st.frames)

    # A strand parked at a branch is captured as just before that branch
    if st.resume == "parked" and frames_now and frames_now[-1].join is not None:
        top = frames_now.pop()
        pc = top.branch
        prefix, counter = top.saved[0], top.saved[1] - 1

    # An open join's continuation has ended with nothing in the window (retire stops at the
    # oldest live strand), so this strand carries the job; its frames restore without joins

    # Every name must hold a filled value
    values: dict[str, Any] = {}
    for name, slot in st.names.to_dict().items():
        s = effective(slot, _FINAL)
        if s.state != SlotState.READY:
            return None
        values[name] = s.value

    frames = [
        {
            "reconv": f.reconv,
            "saved": f.saved,
            "branch": f.branch,
            "kind": f.kind,
            "each": f.each,
            "items": f.items,
            "i": f.i,
            "base": f.base,
            "joined": f.join is not None,
        }
        for f in frames_now
    ]
    state = {
        "pc": pc,
        "prefix": prefix,
        "counter": counter,
        "frames": frames,
        "values": values,
        "stats": dict(job.stats),
        "steps": job.steps,
        "record": job.record,
    }
    return encode_snapshot(state, job.entry, code, manifest)


def apply_prepared(job: Any, state: dict, decision: dict) -> None:
    """Stage validated state completely, then install; never independently copy user values."""
    frames = tuple(
        Frame(**{k: v for k, v in frame.items() if k != "joined"}) for frame in state["frames"]
    )
    names = NameMap({name: const(value) for name, value in state["values"].items()})
    strand = Strand(job, state["pc"], state["prefix"], state["counter"], frames, names)
    stats = {
        **job.stats,
        **state["stats"],
        "resumed": True,
        "snapshot_restore": {"status": decision["status"], "issues": decision["issues"]},
    }
    # Runtime decision caches must be rebuilt under this machine's warning/mode context.
    stats.pop("_persistence_context", None)
    # Local identity is admission context, never portable state. Preserve the
    # caller's pins even through the low-level restore wrapper; drop absent pins.
    for name in ("execution", "execution_epoch", "program_region"):
        stats.pop(name, None)
        if name in job.stats:
            stats[name] = job.stats[name]
    record = state["record"] if job.record is not None else None
    if job.record is not None and record is None:
        stats["record_skipped"] = "resumed without a record"
    # Nothing below consumes unvalidated types or addresses.
    for previous in list(job.strands.values()):
        job.end_strand(previous)
    job.stats = stats
    job.record = record
    job.steps = state["steps"]
    job.snap_at = stats["retired"]
    job.add_strand(strand)


def restore(
    job: Any,
    blob: bytes,
    code: Any = None,
    current_manifest: dict | None = None,
    mode: str = "warn",
) -> bool:
    """Low-level compatibility wrapper; False means denied, never permission to restart.

    Admission uses ``prepare_restore`` directly so absence and denial cannot be
    confused. V2 restoration requires actual code and the current entry manifest.
    """
    if code is None:
        return False
    manifest = job.entry.region.execution if current_manifest is None else current_manifest
    prepared = prepare_restore(blob, job.entry, code, manifest, mode)
    if not prepared["decision"]["allow"]:
        return False
    apply_prepared(job, prepared["state"], prepared["decision"])
    return True


def snapshot_rejection(decision: dict) -> dict:
    """Stable per-job denial DTO shared by both admission paths, not a machine error."""
    issues = decision["issues"]
    message = "; ".join(issue["message"] for issue in issues) or "snapshot is incompatible"
    return {
        "failure": {"kind": "SnapshotIncompatible", "node": None, "message": message},
        "stats": {
            "snapshot_restore": {
                "status": "rejected",
                "code": issues[0]["code"] if issues else decision["status"],
                "issues": issues,
            }
        },
    }


def load_candidate(store: Any, new_key: bytes, old_key: bytes, on_error=None) -> dict:
    """Probe v2 first, v1 only on absence; a read error or legacy hit is a denial."""
    if store is None:
        return {"action": "new", "blob": None, "decision": None}
    for key, legacy in ((new_key, False), (old_key, True)):
        try:
            blob = store.get(key)
        except Exception as exc:  # noqa: BLE001
            if on_error is not None:
                on_error(exc)
            decision = denied(
                "invalid", "store_read_error", "store", f"{type(exc).__name__}: {exc}"
            )
            return {"action": "reject", "blob": None, "decision": decision}
        if blob is MISS or blob is None:
            continue
        if legacy:
            decision = denied(
                "unsupported_version",
                "migration_needed",
                "version",
                "v1 snapshots require offline migration",
            )
            return {"action": "reject", "blob": None, "decision": decision}
        return {"action": "candidate", "blob": blob, "decision": None}
    return {"action": "new", "blob": None, "decision": None}


def load(
    store: Any, key: bytes, on_error: Callable[[BaseException], None] | None = None
) -> bytes | None:
    """The stored snapshot, or None; a failing store read goes to ``on_error`` (else the log)
    and reads as no snapshot."""
    if store is None:
        return None
    try:
        got = store.get(key)
    except Exception as exc:
        if on_error is None:
            log.warning("snapshot read failed: %s", exc, exc_info=exc)
        else:
            on_error(exc)
        return None
    return None if got is MISS or got is None else got
