"""Job state: slots, name maps, frames, strands, inflights, windows, jobs (spec/03 §3.2).

Only the dispatcher thread touches these objects.
"""

import heapq
import itertools
from bisect import insort
from typing import Any

_ids = itertools.count(1)
_seq = itertools.count()


class SlotState:
    """Slot states; ``match`` compares against the dotted names (``case SlotState.READY``)."""

    PENDING = 0
    READY = 1
    SQUASHED = 2


class StrandState:
    RUNNABLE = "runnable"
    PARKED = "parked"
    STALLED = "stalled"
    ENDED = "ended"


class InfState:
    WAITING = "waiting"
    READY = "ready"
    ISSUED = "issued"
    DONE = "done"
    RETIRED = "retired"
    SQUASHED = "squashed"


_SLOT_NAMES = {
    SlotState.PENDING: "pending",
    SlotState.READY: "ready",
    SlotState.SQUASHED: "squashed",
}
_LIVE = (InfState.WAITING, InfState.READY, InfState.ISSUED)


class Slot:
    """One renamed value. A phi slot stands for a write an unresolved region will make.

    ``size`` and ``owner`` are set only on a held slot (``hold``).
    """

    __slots__ = ("value", "state", "waiters", "forward", "phi", "muts", "id", "size", "owner")

    def __init__(self, value: Any = None, state: int = SlotState.PENDING, phi: bool = False):
        self.value = value
        self.state = state
        self.waiters: list = []
        self.forward: Slot | None = None
        self.phi = phi
        # Pending in-place changes: (order key, version slot) of `mutates` calls on this value
        self.muts: list[tuple[tuple, Slot]] = []
        self.id = next(_ids)

    def __repr__(self) -> str:
        return f"<slot {self.id} {'phi ' if self.phi else ''}{_SLOT_NAMES[self.state]}>"


class HeldSlot(Slot):
    """A slot whose value's bytes count toward its job until the slot is freed.

    Freeing may happen on any thread (the collector), so the release is queued on the job's
    ``freed`` deque and applied by the dispatcher.
    """

    __slots__ = ()

    def __del__(self) -> None:
        freed = self.owner.freed
        if freed is not None:
            freed.append((self.owner, self.size))


def hold(slot: Slot, job: "Job", size: int) -> int:
    """``slot``'s value counts ``size`` bytes toward ``job`` while the slot lives; the bytes
    added (0 for an empty value or a slot already held)."""
    if size <= 0 or type(slot) is HeldSlot:
        return 0
    slot.size = size
    slot.owner = job
    slot.__class__ = HeldSlot
    job.bytes += size
    return size


def const(value: Any) -> Slot:
    return Slot(value, SlotState.READY)


def deref(s: Slot) -> Slot:
    while s.forward is not None:
        s = s.forward
    return s


def effective(s: Slot, key: tuple) -> Slot:
    """The version a reader at ``key`` sees: follow resolved phis and older `mutates` versions."""
    s = deref(s)
    while s.muts:
        best = None
        for k, v in s.muts:
            if k < key and v.state != SlotState.SQUASHED and (best is None or k > best[0]):
                best = (k, v)
        if best is None:
            break
        s = deref(best[1])
    return s


class Names:
    """An immutable view of a name map: a shared base dict overlaid by a small delta."""

    __slots__ = ("base", "delta")

    def __init__(self, base: dict, delta: dict):
        self.base = base
        self.delta = delta

    def get(self, name: str) -> Slot | None:
        s = self.delta.get(name)
        return s if s is not None else self.base.get(name)

    def to_dict(self) -> dict[str, Slot]:
        return {**self.base, **self.delta} if self.delta else dict(self.base)

    def keys(self):
        return self.to_dict().keys()

    def items(self):
        return self.to_dict().items()

    def __iter__(self):
        return iter(self.to_dict())

    def __contains__(self, name: str) -> bool:
        return name in self.delta or name in self.base


class NameMap:
    """Name -> slot map: a shared base plus a private delta, folded into a new base when large.

    ``snapshot()`` shares the delta until the next write, so a snapshot costs
    nothing and a write after it copies only the delta.
    """

    __slots__ = ("base", "delta", "shared")

    FOLD = 32

    def __init__(self, base: dict | None = None, delta: dict | None = None, shared: bool = False):
        self.base: dict[str, Slot] = base if base is not None else {}
        self.delta: dict[str, Slot] = delta if delta is not None else {}
        self.shared = shared

    @classmethod
    def of(cls, view: Names, extra: dict | None = None) -> "NameMap":
        """A map starting from ``view``, with ``extra`` names written over it."""
        if not extra:
            return cls(view.base, view.delta, shared=True)
        nm = cls(view.base, {**view.delta, **extra})
        if len(nm.delta) > cls.FOLD:
            nm.fold()
        return nm

    def get(self, name: str) -> Slot | None:
        s = self.delta.get(name)
        return s if s is not None else self.base.get(name)

    def set(self, name: str, slot: Slot) -> None:
        if self.shared:
            self.delta = dict(self.delta)
            self.shared = False
        self.delta[name] = slot
        if len(self.delta) > self.FOLD:
            self.fold()

    def fold(self) -> None:
        self.base = {**self.base, **self.delta}
        self.delta = {}
        self.shared = False

    def snapshot(self) -> Names:
        self.shared = True
        return Names(self.base, self.delta)

    def to_dict(self) -> dict[str, Slot]:
        return {**self.base, **self.delta}


class Join:
    """The phis a parked region (branch arm, `each` iteration) resolves where it reconverges."""

    __slots__ = ("phis", "table", "resolved", "all_names")

    def __init__(self, phis: dict[str, Slot], table: dict | None, all_names: bool):
        self.phis = phis
        self.table = table
        self.resolved = False
        self.all_names = all_names


class Frame:
    """A region a strand is inside: where it rejoins, the key to restore, and its join if parked.

    Frames are never changed once pushed; a strand's frames are a tuple, so
    snapshots and new strands share them.
    """

    __slots__ = ("reconv", "saved", "join", "branch", "kind", "each", "items", "i", "base")

    def __init__(
        self,
        reconv,
        saved,
        join=None,
        branch=None,
        kind="branch",
        each=None,
        items=None,
        i=0,
        base=None,
    ):
        self.reconv = reconv
        self.saved = saved
        self.join = join
        self.branch = branch
        self.kind = kind
        self.each = each
        self.items = items
        self.i = i
        self.base = base

    def replace(self, **changes: Any) -> "Frame":
        f = Frame(
            self.reconv,
            self.saved,
            self.join,
            self.branch,
            self.kind,
            self.each,
            self.items,
            self.i,
            self.base,
        )
        for k, v in changes.items():
            setattr(f, k, v)
        return f


class Strand:
    __slots__ = (
        "id",
        "job",
        "pc",
        "prefix",
        "counter",
        "frames",
        "names",
        "state",
        "resume",
        "queued",
        "spawn",
    )

    def __init__(
        self,
        job,
        pc: int,
        prefix: tuple,
        counter: int,
        frames: tuple,
        names: NameMap | None,
        spawn: Any = None,
    ):
        self.id = next(_ids)
        self.job = job
        self.pc = pc
        self.prefix = prefix
        self.counter = counter
        self.frames = frames
        self.names = names
        self.state = StrandState.RUNNABLE
        # Parked or stalled on a control instruction: re-run it when its operand is ready
        self.resume: str | bool = False
        # In the job's runnable list
        self.queued = False
        # Lazy `each` iterations this strand starts (None for an ordinary strand)
        self.spawn = spawn

    def next_key(self) -> tuple:
        return (*self.prefix, self.counter)

    def __repr__(self) -> str:
        what = "spawner" if self.spawn is not None else "strand"
        return f"<{what} {self.id} pc={self.pc} key={self.next_key()} {self.state}>"


class Inflight:
    __slots__ = (
        "id",
        "job",
        "instr",
        "key",
        "inputs",
        "outs",
        "versions",
        "state",
        "fault",
        "snapshot",
        "item",
        "copy",
        "values",
        "result",
        "kept",
        "mutated",
        "t_issue",
        "t_done",
        "store_key",
        "hit",
        "control",
        "dig",
        "reused",
    )

    def __init__(self, job, instr, key):
        self.id = next(_ids)
        self.job = job
        self.instr = instr
        self.key = key
        self.inputs: list[tuple[str, Slot]] = []
        self.outs: list[Slot | None] = []
        # `mutates` ports -> (slot before, version slot after the change); None without `mutates`
        self.versions: dict[str, tuple[Slot, Slot]] | None = None
        self.state = InfState.WAITING
        self.fault: dict | None = None
        self.snapshot = None
        self.item = None
        self.copy = False
        self.values: dict | None = None
        self.result = None
        # A copy of the result taken at settle (for the record and positional stores), and
        # recorded copies of changed `mutates` inputs
        self.kept = None
        self.mutated = None
        self.t_issue = 0.0
        self.t_done = 0.0
        self.store_key = None
        self.hit = False
        self.control = False
        self.dig: str | None = None
        # Completed with a result kept from a squashed run of the same statement
        self.reused = False

    def __lt__(self, other: "Inflight") -> bool:
        return self.key < other.key

    def __repr__(self) -> str:
        return f"<inflight {self.instr.node or self.instr.op} key={self.key} {self.state}>"


class Window:
    """A job's inflights by order key: a list with a moving head (retire pops the head)."""

    __slots__ = ("items", "head")

    def __init__(self) -> None:
        self.items: list[Inflight] = []
        self.head = 0

    def add(self, inf: Inflight) -> None:
        items = self.items
        if len(items) == self.head or items[-1].key < inf.key:
            items.append(inf)
        else:
            insort(items, inf, lo=self.head)

    def first(self) -> Inflight:
        return self.items[self.head]

    def pop_first(self) -> Inflight:
        x = self.items[self.head]
        self.head += 1
        if self.head > 256 and self.head * 2 > len(self.items):
            del self.items[: self.head]
            self.head = 0
        return x

    def cut_after(self, key: tuple) -> list[Inflight]:
        """Remove and return every inflight with a key larger than ``key``."""
        items = self.items
        lo, hi = self.head, len(items)
        while lo < hi:
            mid = (lo + hi) // 2
            if items[mid].key <= key:
                lo = mid + 1
            else:
                hi = mid
        cut = items[lo:]
        del items[lo:]
        return cut

    def clear(self) -> list[Inflight]:
        rest = self.items[self.head :]
        self.items = []
        self.head = 0
        return rest

    def __len__(self) -> int:
        return len(self.items) - self.head

    def __bool__(self) -> bool:
        return len(self.items) > self.head

    def __iter__(self):
        return iter(self.items[self.head :])


class Job:
    __slots__ = (
        "id",
        "key",
        "entry",
        "inputs",
        "strands",
        "sheap",
        "runnable",
        "window",
        "wheap",
        "unfinished",
        "issued",
        "final_names",
        "final_key",
        "failure",
        "stats",
        "record",
        "ended",
        "start_time",
        "paused",
        "held",
        "handle",
        "steps",
        "bytes",
        "sampled",
        "auto_key",
        "snap_at",
        "snap_off",
        "inputs_dig",
        "freed",
        "memo",
    )

    def __init__(self, key: str, entry, inputs: dict):
        self.id = next(_ids)
        self.key = key
        self.entry = entry
        self.inputs = inputs
        # Live strands by id, a heap of (next key, seq, strand) with stale entries, runnable ones
        self.strands: dict[int, Strand] = {}
        self.sheap: list = []
        self.runnable: list[Strand] = []
        # Window by order key, and a heap of (key, seq, inflight) for unfinished or faulted ones
        self.window = Window()
        self.wheap: list = []
        self.unfinished = 0
        # Issued and not yet done
        self.issued = 0
        self.final_names = None
        self.final_key: tuple | None = None
        self.failure: dict | None = None
        self.stats: dict[str, Any] = {
            "calls": 0,
            "squashed": 0,
            "retired": 0,
            "window_peak": 0,
            "strands_peak": 0,
            "inflight_peak": 0,
            "mutates_copies": 0,
            "parks": 0,
            "reused": 0,
            "bytes_produced": 0,
            "bytes_peak": 0,
        }
        self.record: list | None = None
        self.ended = False
        self.start_time = 0.0
        self.paused = False
        # Ready inflights held while the job is paused
        self.held: list[Inflight] = []
        self.handle = None
        self.steps = 0
        self.bytes = 0
        self.sampled = False
        self.auto_key = False
        self.snap_at = 0
        self.snap_off = False
        # Digest of the inputs as given, taken at start; None when weak (or not taken)
        self.inputs_dig: bytes | None = None
        # The dispatcher's queue of (job, bytes) released by freed held slots
        self.freed = None
        # Results kept from squashed statements (`retire.faults.Memo`), made at the first one
        self.memo = None

    # ------------------------------------------------------------ strands
    def add_strand(self, st: Strand) -> None:
        self.strands[st.id] = st
        heapq.heappush(self.sheap, (st.next_key(), next(_seq), st))
        if len(self.strands) > self.stats["strands_peak"]:
            self.stats["strands_peak"] = len(self.strands)
        if st.state == StrandState.RUNNABLE:
            self.mark_runnable(st)

    def end_strand(self, st: Strand) -> None:
        st.state = StrandState.ENDED
        self.strands.pop(st.id, None)

    def mark_runnable(self, st: Strand) -> None:
        if not st.queued:
            st.queued = True
            self.runnable.append(st)

    def take_runnable(self) -> list[Strand]:
        got, self.runnable = self.runnable, []
        for st in got:
            st.queued = False
        return got

    def oldest_live_key(self) -> tuple | None:
        h = self.sheap
        while h:
            k, _, st = h[0]
            if st.state == StrandState.ENDED:
                heapq.heappop(h)
                continue
            cur = st.next_key()
            if cur != k:
                heapq.heapreplace(h, (cur, next(_seq), st))
                continue
            return k
        return None

    # ------------------------------------------------------------ window
    def add(self, inf: Inflight) -> None:
        self.window.add(inf)
        heapq.heappush(self.wheap, (inf.key, next(_seq), inf))
        if len(self.window) > self.stats["window_peak"]:
            self.stats["window_peak"] = len(self.window)

    def issue(self, inf: Inflight) -> None:
        inf.state = InfState.ISSUED
        self.issued += 1
        if self.issued > self.stats["inflight_peak"]:
            self.stats["inflight_peak"] = self.issued

    def oldest_open_key(self) -> tuple | None:
        """The key of the oldest inflight still unfinished, or done with a fault not yet taken."""
        h = self.wheap
        while h:
            x = h[0][2]
            if x.state in _LIVE or (x.state == InfState.DONE and x.fault is not None):
                return x.key
            heapq.heappop(h)
        return None
