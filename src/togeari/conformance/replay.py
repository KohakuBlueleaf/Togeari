"""Replay a machine job through kohakunode's reference interpreter (spec/07 §7.1).

The machine records every node call of a sampled job in retire order (its
sequential order). The reference interpreter runs the same job with a call hook
that pops the next record and checks node name and input digest. Outputs, or
failure kind and node, must be equal.

Two modes:

- ``replay``: every call returns (a copy of) the recorded result, or raises the
  recorded error.
- ``reexecute``: calls of nodes marked pure run the node function and compare its
  result with the recorded one; every other call is replayed.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

from kohakunode import Interpreter, KirRuntimeError, NodeFailure

from togeari.core.contracts.digest import digest
from togeari.core.contracts.io import copy_value

MODES = ("replay", "reexecute")

# Instructions the reference interpreter may execute per job, by default
STEP_BUDGET = 1_000_000


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    reference: Any = None
    machine: Any = None
    details: list = field(default_factory=list)


_error_types: dict[str, type] = {}


def _error(kind: str, message: str) -> Exception:
    """An exception whose class name is the recorded kind."""
    cls = _error_types.get(kind)
    if cls is None:
        cls = type(kind, (Exception,), {})
        _error_types[kind] = cls
    return cls(message)


def _fresh(value: Any) -> Any:
    """A copy of a recorded value, so the reference never changes the record."""
    try:
        return copy_value(value)
    except Exception:  # noqa: BLE001
        return value


def pure_test(pure: Callable[[str], bool] | Collection[str] | None) -> Callable[[str], bool]:
    """``pure`` as a predicate over node names: None (no node), a predicate, or names."""
    match pure:
        case None:
            return lambda name: False
        case str():
            raise TypeError("pure: give a collection of node names or a predicate, not a str")
        case _ if callable(pure):
            return pure
        case _:
            return frozenset(pure).__contains__


def assign_in_place(obj: Any, after: Any) -> bool:
    """Make ``obj`` equal to ``after`` without replacing it; False if the type cannot be."""
    match obj:
        case list():
            obj[:] = after
        case dict() | set():
            obj.clear()
            obj.update(after)
        case _ if hasattr(obj, "__dict__"):
            obj.__dict__.clear()
            obj.__dict__.update(after.__dict__)
        case _:
            return obj == after
    return True


class Mismatch(KirRuntimeError):
    """The reference and the machine disagree; never caught by `@try`."""


class _Hook:
    """The reference's call hook over one job's record."""

    def __init__(self, record: list, reexecute: bool, pure: Callable, engine_errors: bool):
        self.queue = list(record)
        self.pos = 0
        self.reexecute = reexecute
        self.pure = pure
        self.engine_errors = engine_errors
        self.notes: list[str] = []

    def __call__(self, name: str, ins: dict, call: Callable[[], Any]) -> Any:
        if self.pos >= len(self.queue):
            raise Mismatch(f"reference calls {name!r} after the machine's last record")
        node, dig, kind, value, mutated = self.queue[self.pos]
        self.pos += 1
        n = self.pos
        if node != name:
            raise Mismatch(f"call {n}: reference {name!r}, machine {node!r}")
        if digest(ins).hex() != dig:
            raise Mismatch(f"call {n} ({name}): input digests differ")

        if self.reexecute and self.pure(name):
            return self.rerun(n, name, ins, call, kind, value, mutated)
        if kind == "error":
            raise _error(value["kind"], value["message"])

        # In-place changes of `mutates` nodes happen to the reference's objects too
        for port, after in (mutated or {}).items():
            if not assign_in_place(ins[port], _fresh(after)):
                raise Mismatch(f"call {n} ({name}): cannot replay the change of {port!r}")
        return _fresh(value)

    def rerun(self, n: int, name: str, ins: dict, call: Callable, kind: str, value, mutated):
        """Run a pure node and hold the machine's result (or failure) against it."""
        try:
            got = call()
        except Exception as exc:
            mine = type(exc).__name__
            match kind:
                case "error" if mine == value["kind"]:
                    raise
                case "error" if self.engine_errors:
                    self.notes.append(
                        f"call {n} ({name}): {value['kind']} kept as an engine failure"
                    )
                    raise _error(value["kind"], value["message"]) from None
                case "error":
                    raise Mismatch(
                        f"call {n} ({name}): machine failed with {value['kind']}, node with {mine}"
                    ) from None
                case _:
                    raise Mismatch(
                        f"call {n} ({name}): node failed with {mine}, machine has a result"
                    ) from None

        # The node succeeds: a recorded failure came from the engine side, or is wrong
        if kind == "error":
            if not self.engine_errors:
                raise Mismatch(f"call {n} ({name}): machine failed with {value['kind']}, node ok")
            self.notes.append(f"call {n} ({name}): {value['kind']} kept as an engine failure")
            raise _error(value["kind"], value["message"])

        # Results and in-place changes equal to the node's own
        if digest(got) != digest(value):
            raise Mismatch(f"call {n} ({name}): machine result differs from the node's")
        for port, after in (mutated or {}).items():
            if digest(ins[port]) != digest(after):
                raise Mismatch(f"call {n} ({name}): the change of {port!r} differs")
        return got


def replay(
    job_program: Any,
    record: list,
    inputs: dict,
    mode: str = "replay",
    pure: Callable[[str], bool] | Collection[str] | None = None,
    engine_errors: bool = True,
    step_budget: int = STEP_BUDGET,
) -> tuple[str, Any, list[str]]:
    """Run the reference over one job's record.

    ``mode``: ``replay`` or ``reexecute`` (``pure`` names the nodes run again).
    ``engine_errors``: under ``reexecute``, a recorded failure of a pure node the node
    itself does not raise is kept as an engine failure (an injected or engine-side
    error); False makes it a mismatch. ``step_budget``: instructions the reference may
    execute before it fails.

    Returns ``("ok", outputs, notes)``, ``("error", {kind, node}, notes)`` or
    ``("mismatch", reason, notes)``.
    """
    match mode:
        case "replay" | "reexecute":
            pass
        case _:
            raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    hook = _Hook(record, mode == "reexecute", pure_test(pure), engine_errors)

    interp = Interpreter(job_program.written, job_program.env, hook=hook, step_budget=step_budget)
    try:
        if job_program.entry is None:
            interp.run()
            out = {k: v for k, v in interp.source_values().items()}
        else:
            out = interp.call(job_program.entry, inputs)
    except Mismatch as exc:
        return "mismatch", str(exc), hook.notes
    except NodeFailure as exc:
        return "error", {"kind": type(exc.error).__name__, "node": exc.node}, hook.notes
    except KirRuntimeError as exc:
        err = {"kind": "KirRuntimeError", "node": None, "message": str(exc)}
        return "error", err, hook.notes
    if hook.pos != len(hook.queue):
        reason = f"machine recorded {len(hook.queue)} calls, reference made {hook.pos}"
        return "mismatch", reason, hook.notes
    return "ok", out, hook.notes


def conform(
    job_program: Any,
    job_record: Any,
    inputs: dict,
    mode: str = "replay",
    pure: Callable[[str], bool] | Collection[str] | None = None,
    engine_errors: bool = True,
    step_budget: int = STEP_BUDGET,
) -> Verdict:
    """Compare one machine job record (with its ``record``) against the reference."""
    rec = job_record.record
    if rec is None:
        return Verdict(False, "job was not recorded")
    status, ref, notes = replay(job_program, rec, inputs, mode, pure, engine_errors, step_budget)
    match status:
        case "mismatch":
            return Verdict(False, ref, details=notes)
        case "error":
            if job_record.ok:
                reason = "reference failed, machine succeeded"
                return Verdict(False, reason, ref, job_record.outputs, notes)
            mine = job_record.failure or {}
            if mine.get("kind") != ref["kind"] or (
                ref["kind"] != "KirRuntimeError" and mine.get("node") != ref["node"]
            ):
                return Verdict(False, "failures differ", ref, mine, notes)
            return Verdict(True, "both failed alike", ref, mine, notes)
    if not job_record.ok:
        reason = "machine failed, reference succeeded"
        return Verdict(False, reason, ref, job_record.failure, notes)
    if digest(ref) != digest(job_record.outputs):
        return Verdict(False, "outputs differ", ref, job_record.outputs, notes)
    return Verdict(True, "", ref, job_record.outputs, notes)
