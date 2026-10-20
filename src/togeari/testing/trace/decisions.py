"""The decision log: ``(kind, job key, region hash, address offset, order key, lane, unit)`` for
every scheduling decision of one machine, in order (docs/internals/verification/trace.md).

It is a synchronous bus listener on the dispatcher thread, so it sees every event in order and
never drops one. Jobs are named by key and statements by region hash and offset, so a log does
not depend on object ids or load order.
"""

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

# Bus event kinds the log listens to, and the decision kind each becomes
KINDS = {
    "job.admit": "admit",
    "stmt.fetch": "fetch",
    "stmt.issue": "issue",
    "lane.submit": "submit",
    "stmt.done": "writeback",
    "stmt.squash": "squash",
    "stmt.retire": "retire",
    "fault.taken": "fault",
    "job.end": "end",
}


class Decision(NamedTuple):
    kind: str
    job: str
    region: str | None
    offset: int | None
    key: tuple
    lane: str | None
    unit: str | None

    def to_json(self) -> list:
        return [self.kind, self.job, self.region, self.offset, list(self.key), self.lane, self.unit]

    @classmethod
    def from_json(cls, row: list) -> "Decision":
        kind, job, region, offset, key, lane, unit = row
        return cls(kind, job, region, offset, _tuple(key), lane, unit)


def _tuple(key: Any) -> tuple:
    """An order key from JSON: nested lists back to tuples."""
    return tuple(_tuple(k) if isinstance(k, list) else k for k in key)


class Mismatch(NamedTuple):
    """Where two logs first differ: the index and both decisions there (None past an end)."""

    index: int
    mine: Decision | None
    other: Decision | None

    def __str__(self) -> str:
        return f"decision {self.index}: {self.mine} != {self.other}"


class DecisionLog:
    """Decisions of one machine; ``attach(machine)`` starts recording, ``detach`` stops."""

    def __init__(self, decisions: Iterable[Decision] = ()):
        self.decisions: list[Decision] = list(decisions)
        self._machine: Any = None
        self._keys: dict[int, str] = {}
        self._places: dict[int, tuple] = {}

    # ------------------------------------------------------------ recording
    def attach(self, machine: Any) -> "DecisionLog":
        self._machine = machine
        machine.bus.listen(self._on_event, KINDS)
        return self

    def detach(self) -> None:
        if self._machine is not None:
            self._machine.bus.unlisten(self._on_event)
            self._machine = None

    def _place(self, addr: int | None) -> tuple:
        """(region hash, offset) of a code address, cached per address."""
        if addr is None:
            return (None, None)
        got = self._places.get(addr)
        if got is None:
            region = self._machine.code.region_of(addr)
            got = self._places[addr] = (region.semantic_hash, addr - region.start)
        return got

    def _on_event(self, kind: str, f: dict) -> None:
        add = self.decisions.append
        match KINDS[kind]:
            case "admit":
                self._keys[f["job"]] = f["key"]
                entry = self._machine.code.entries[f["entry"]]
                add(Decision("admit", f["key"], *self._place(entry.addr), (), None, None))
            case "end":
                entry = self._machine.code.entries[f["entry"]]
                outcome = "ok" if f["ok"] else (f["failure"] or {}).get("kind")
                key = self._keys.pop(f["job"], f["key"])
                add(Decision("end", key, *self._place(entry.addr), (), None, outcome))
            case "submit":
                for job_key, addr, key in f["work"]:
                    add(Decision("submit", job_key, *self._place(addr), key, f["lane"], f["unit"]))
            case "fault":
                job = self._keys.get(f["job"])
                unit = f.get("error_kind")
                add(Decision("fault", job, *self._place(f["addr"]), f["key"], None, unit))
            case "writeback":
                job = self._keys.get(f["job"])
                place = self._place(f["addr"])
                add(Decision("writeback", job, *place, f["key"], f["lane"], f.get("unit")))
            case other:
                job = self._keys.get(f["job"])
                add(Decision(other, job, *self._place(f["addr"]), f["key"], f["lane"], None))

    # ------------------------------------------------------------ comparing
    def digest(self) -> str:
        """SHA-256 of the log's canonical JSON lines."""
        h = hashlib.sha256()
        for d in self.decisions:
            h.update(json.dumps(d.to_json(), separators=(",", ":")).encode())
            h.update(b"\n")
        return h.hexdigest()

    def first_mismatch(self, other: "DecisionLog") -> Mismatch | None:
        """The first index where the logs differ, or None when they are identical."""
        a, b = self.decisions, other.decisions
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return Mismatch(i, x, y)
        if len(a) != len(b):
            i = min(len(a), len(b))
            return Mismatch(i, a[i] if i < len(a) else None, b[i] if i < len(b) else None)
        return None

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.decisions:
            out[d.kind] = out.get(d.kind, 0) + 1
        return out

    # ------------------------------------------------------------ files
    def write(self, path: str | Path) -> None:
        """One JSON list per line."""
        lines = (json.dumps(d.to_json(), separators=(",", ":")) for d in self.decisions)
        Path(path).write_text("".join(f"{x}\n" for x in lines), encoding="utf-8")

    @classmethod
    def read(cls, path: str | Path) -> "DecisionLog":
        rows = Path(path).read_text(encoding="utf-8").splitlines()
        return cls(Decision.from_json(json.loads(r)) for r in rows if r)

    def __len__(self) -> int:
        return len(self.decisions)

    def __iter__(self):
        return iter(self.decisions)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DecisionLog) and self.decisions == other.decisions
