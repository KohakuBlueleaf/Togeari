"""Snapshot v2 wire envelope and shared, mutation-free Python/native preflight.

Snapshots are trusted pickle transport. Validation protects runtime consistency,
not against code execution by an untrusted pickle. User values are never copied
independently, so their unpickled alias graph survives preparation unchanged.
"""

import pickle
from typing import Any

from togeari.core.program.decode.analysis import EXIT
from togeari.core.program.decode.instr import Op
from togeari.core.program.execution import compatibility

SCHEMA = "togeari.snapshot"
VERSION = 2
ADDRESSING = "region-relative-v1"
I64_MIN = -(2**63)
I64_MAX = 2**63 - 1
_STATE_KEYS = {"pc", "prefix", "counter", "frames", "values", "stats", "steps", "record"}
_FRAME_KEYS = {"reconv", "saved", "branch", "kind", "each", "items", "i", "base", "joined"}
_COUNTERS = {
    "calls",
    "squashed",
    "retired",
    "window_peak",
    "strands_peak",
    "inflight_peak",
    "mutates_copies",
    "parks",
    "reused",
    "bytes_produced",
    "bytes_peak",
}
_OPTIONAL_COUNTERS = {
    "snapshots",
    "collect_captures",
    "collect_finalizations",
    "collect_peak_items",
    "execution_epoch",
    "program_region",
    "bytes",
    "steps",
}


class InvalidState(ValueError):
    def __init__(self, path: str, message: str):
        self.path = path
        super().__init__(message)


def denied(status: str, code: str, path: str, message: str) -> dict:
    return {
        "status": status,
        "allow": False,
        "issues": [{"code": code, "path": path, "message": message}],
    }


def _integer(value: Any, path: str, *, nonnegative: bool = False) -> int:
    if type(value) is not int or not I64_MIN <= value <= I64_MAX:
        raise InvalidState(path, "expected an exact signed-i64 integer")
    if nonnegative and value < 0:
        raise InvalidState(path, "expected a nonnegative integer")
    return value


def _key(value: Any, path: str) -> tuple:
    if type(value) is not tuple:
        raise InvalidState(path, "expected a tuple order key")
    for index, part in enumerate(value):
        _integer(part, f"{path}[{index}]", nonnegative=True)
    return value


def _mapping(value: Any, path: str) -> dict:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise InvalidState(path, "expected an exact string-keyed dictionary")
    return value


def _shape(value: Any, keys: set[str], path: str) -> dict:
    value = _mapping(value, path)
    if set(value) != keys:
        raise InvalidState(path, f"expected fields {sorted(keys)}")
    return value


def _record(value: Any) -> None:
    if value is None:
        return
    if type(value) is not list:
        raise InvalidState("state.record", "expected a list or None")
    for index, entry in enumerate(value):
        path = f"state.record[{index}]"
        if type(entry) is not tuple or len(entry) != 5:
            raise InvalidState(path, "expected a five-element call record tuple")
        node, digest, kind, result, mutated = entry
        if type(node) is not str or (digest is not None and type(digest) is not str):
            raise InvalidState(path, "node must be a string; digest must be a string or None")
        if type(kind) is not str or kind not in ("ok", "error"):
            raise InvalidState(path, "record kind must be ok or error")
        if mutated is not None:
            _mapping(mutated, path + ".mutated")
        if kind == "error":
            _shape(result, {"kind", "node", "message"}, path + ".error")
            if type(result["kind"]) is not str or type(result["message"]) is not str:
                raise InvalidState(path, "error kind/message must be strings")
            if result["node"] is not None and type(result["node"]) is not str:
                raise InvalidState(path, "error node must be string or None")


def _address(value: Any, region: Any, path: str, *, relative: bool, nullable=False, exit_ok=False):
    if value is None and nullable:
        return None
    value = _integer(value, path)
    if exit_ok and value == EXIT:
        return EXIT
    absolute = value + region.start if relative else value
    if not region.start <= absolute < region.end:
        raise InvalidState(path, "address is outside the matching entry region")
    return absolute


def _state(raw: Any, entry: Any, code: Any, *, relative: bool) -> dict:
    state = _shape(raw, _STATE_KEYS, "state")
    region = entry.region
    result = dict(state)
    result["pc"] = _address(state["pc"], region, "state.pc", relative=relative)
    _key(state["prefix"], "state.prefix")
    _integer(state["counter"], "state.counter", nonnegative=True)
    _integer(state["steps"], "state.steps", nonnegative=True)
    _mapping(state["values"], "state.values")
    stats = _mapping(state["stats"], "state.stats")
    if not stats.keys() >= _COUNTERS:
        raise InvalidState("state.stats", "missing runtime counters")
    # Only fields interpreted by the runtimes have a structural type contract.
    # Plugin extras remain arbitrary trusted-pickle values, with aliases intact.
    for name in _COUNTERS | (_OPTIONAL_COUNTERS & stats.keys()):
        _integer(stats[name], f"state.stats.{name}", nonnegative=True)
    if "resumed" in stats and type(stats["resumed"]) is not bool:
        raise InvalidState("state.stats.resumed", "expected an exact boolean")
    if "program" in stats and type(stats["program"]) is not str:
        raise InvalidState("state.stats.program", "expected an exact string")
    _record(state["record"])
    if type(state["frames"]) is not list:
        raise InvalidState("state.frames", "expected a list")
    frames = []
    for index, raw_frame in enumerate(state["frames"]):
        path = f"state.frames[{index}]"
        frame = dict(_shape(raw_frame, _FRAME_KEYS, path))
        if type(frame["kind"]) is not str or frame["kind"] not in ("branch", "iter"):
            raise InvalidState(path + ".kind", "unknown frame kind")
        if type(frame["joined"]) is not bool:
            raise InvalidState(path + ".joined", "expected an exact boolean")
        saved = frame["saved"]
        if type(saved) is not tuple or len(saved) != 2:
            raise InvalidState(path + ".saved", "expected (order-key tuple, counter)")
        _key(saved[0], path + ".saved[0]")
        _integer(saved[1], path + ".saved[1]", nonnegative=True)
        _integer(frame["i"], path + ".i", nonnegative=True)
        for field in ("reconv", "branch", "each"):
            frame[field] = _address(
                frame[field],
                region,
                path + "." + field,
                relative=relative,
                nullable=field in ("reconv", "each"),
                exit_ok=field == "reconv",
            )
        instruction = code.mem[frame["branch"]]
        if frame["kind"] == "branch":
            if instruction.op not in (Op.BRANCH, Op.SWITCH, Op.EACH):
                raise InvalidState(
                    path + ".branch", "branch frame does not name a control instruction"
                )
            if (
                frame["each"] is not None
                or frame["items"] is not None
                or frame["base"] is not None
                or frame["i"] != 0
            ):
                raise InvalidState(path, "branch frame contains iteration state")
            expected = region.tables.reconv.get(frame["branch"])
            if frame["reconv"] != expected:
                raise InvalidState(path + ".reconv", "branch reconvergence does not match code")
        else:
            if frame["each"] != frame["branch"] or instruction.op != Op.EACH:
                raise InvalidState(
                    path + ".each", "iteration frame does not name its each instruction"
                )
            if instruction.collect_target is not None:
                raise InvalidState(path, "active collector snapshots are not supported")
            if type(frame["items"]) is not list or not 0 <= frame["i"] < len(frame["items"]):
                raise InvalidState(
                    path + ".items", "iteration index must select a snapshotted list item"
                )
            _key(frame["base"], path + ".base")
            expected = (
                instruction.iter_end if frame["joined"] else region.tables.reconv.get(frame["each"])
            )
            if frame["reconv"] != expected:
                raise InvalidState(path + ".reconv", "iteration reconvergence does not match code")
        frames.append(frame)
    result["frames"] = frames
    return result


def encode_snapshot(raw_state: dict, entry: Any, code: Any, manifest: dict) -> bytes:
    """Validate an absolute state, then serialize only region-relative address fields."""
    decision = compatibility(manifest, manifest, "warn")
    if not decision["allow"]:
        raise ValueError(
            "snapshot execution is not persistable: "
            + "; ".join(issue["message"] for issue in decision["issues"])
        )
    state = _state(raw_state, entry, code, relative=False)
    start = entry.region.start
    state["pc"] -= start
    for frame in state["frames"]:
        for name in ("reconv", "branch", "each"):
            if frame[name] is not None and frame[name] != EXIT:
                frame[name] -= start
    return pickle.dumps(
        {
            "schema": SCHEMA,
            "version": VERSION,
            "addressing": ADDRESSING,
            "execution": manifest,
            "state": state,
        },
        protocol=5,
    )


def prepare_restore(blob: bytes, entry: Any, code: Any, current_manifest: dict, mode: str) -> dict:
    """Return decision plus detached, absolute state, without touching any live job/code."""
    try:
        if type(blob) is not bytes:
            raise InvalidState("snapshot", "snapshot payload must be bytes")
        envelope = pickle.loads(blob)
        if (
            type(envelope) is dict
            and type(envelope.get("version")) is int
            and envelope["version"] == 1
        ):
            return {
                "decision": denied(
                    "unsupported_version",
                    "migration_needed",
                    "version",
                    "v1 snapshots require offline migration",
                ),
                "state": None,
            }
        if (
            type(envelope) is not dict
            or type(envelope.get("version")) is not int
            or envelope.get("version") != VERSION
        ):
            return {
                "decision": denied(
                    "unsupported_version",
                    "snapshot_version",
                    "version",
                    "unsupported snapshot version",
                ),
                "state": None,
            }
        _shape(envelope, {"schema", "version", "addressing", "execution", "state"}, "snapshot")
        if envelope["schema"] != SCHEMA or envelope["addressing"] != ADDRESSING:
            raise InvalidState("snapshot", "invalid snapshot schema or addressing")
        decision = compatibility(envelope["execution"], current_manifest, mode)
        if not decision["allow"]:
            return {"decision": decision, "state": None}
        state = _state(envelope["state"], entry, code, relative=True)
        # Joined iteration continuation already ran to completion before capture.
        for frame in state["frames"]:
            if frame["joined"] and frame["kind"] == "iter":
                frame["reconv"] = entry.region.tables.reconv.get(frame["each"])
        return {"decision": decision, "state": state}
    except InvalidState as exc:
        decision = denied("invalid", "snapshot_structure", exc.path, str(exc))
    except Exception as exc:  # noqa: BLE001
        decision = denied("invalid", "snapshot_decode", "snapshot", f"{type(exc).__name__}: {exc}")
    return {"decision": decision, "state": None}
