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
import pickle
from collections.abc import Callable
from typing import Any

from togeari.core.contracts.digest import digest
from togeari.core.contracts.io import MISS
from togeari.core.runtime.state import Frame, NameMap, SlotState, Strand, const, effective

log = logging.getLogger("togeari.snapshot")
_FINAL = (float("inf"),)


def snapshot_key(region_hash: str, job_key: str, inputs_digest: bytes) -> bytes:
    return digest(("snapshot", region_hash, job_key, inputs_digest))


def capture(job: Any) -> bytes | None:
    """The job's state as bytes, or None off a boundary; raises when a value cannot pickle."""
    live = list(job.strands.values())
    if job.window or len(live) != 1 or live[0].spawn is not None:
        return None
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
    return pickle.dumps(state, protocol=5)


def restore(job: Any, blob: bytes) -> bool:
    """Put a captured state back into a fresh job; False if it cannot be read."""
    try:
        state = pickle.loads(blob)
    except Exception:  # noqa: BLE001
        log.warning("snapshot of job %s unreadable; starting over", job.key)
        return False

    # Unpickled values belong to this job alone: no copy
    frames = tuple(Frame(**d) for d in state["frames"])
    names = NameMap({n: const(v) for n, v in state["values"].items()})
    for st in list(job.strands.values()):
        job.end_strand(st)
    job.add_strand(
        Strand(job, state["pc"], tuple(state["prefix"]), state["counter"], frames, names)
    )
    job.stats.update(state["stats"])
    job.stats["resumed"] = True
    job.steps = state.get("steps", 0)
    job.snap_at = job.stats.get("retired", 0)

    # A recorded job continues the record of the run it resumes; without one it is not recorded
    if job.record is not None:
        job.record = state.get("record")
        if job.record is None:
            job.stats["record_skipped"] = "resumed without a record"
    return True


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
