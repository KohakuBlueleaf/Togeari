"""The telemetry ring of the Python core: records (``togeari.core.contracts.telemetry``) written
by the dispatcher and the lanes, drained by a reader on its own thread. A full ring drops the
record and counts it, so a writer never waits on a reader. ``togeari._rs.TelemetryRing`` is the
same ring in Rust, with the same methods and record bytes.
"""

import threading
import time

from togeari.core.contracts.telemetry import (
    DEFAULT_RECORDS,
    QUEUED_SAT,
    SERVICE_SAT,
    SIZE,
    pack,
)

# The id every name past the table's end shares (real ids are 1 to OVERFLOW - 1)
OVERFLOW = 0xFFFF
MAX_RECORDS = 1 << 30


class Names:
    """Lane and unit ids by name: ``(kind, id, name, appeared_ns, left_ns)`` rows."""

    def __init__(self) -> None:
        self.ids: dict[tuple[str, str], int] = {}
        self.rows: list[list] = []
        self.next = {"lane": 1, "unit": 1}

    def id(self, kind: str, name: str, t_ns: int) -> int:
        key = (kind, name)
        got = self.ids.get(key)
        if got is not None:
            return got

        # A new name; past the table's end every new name gets OVERFLOW
        n = self.next[kind]
        if n >= OVERFLOW:
            return OVERFLOW
        self.next[kind] = n + 1
        self.ids[key] = n
        self.rows.append([kind, n, name, t_ns, None])
        return n

    def leave(self, kind: str, name: str, t_ns: int) -> None:
        got = self.ids.get((kind, name))
        if got is None:
            return
        for row in self.rows:
            if row[1] == got and row[0] == kind:
                row[4] = t_ns


class Ring:
    """A bounded ring of records (``capacity`` rounded up to a power of two)."""

    def __init__(self, capacity: int = DEFAULT_RECORDS):
        cap = 1
        while cap < min(max(1, capacity), MAX_RECORDS):
            cap <<= 1
        self.capacity = cap
        self.buf = bytearray(cap * SIZE)
        self.head = 0
        self.tail = 0
        self.written = 0
        self.dropped = 0
        self.saturated = 0
        self.names = Names()
        self.epoch_ns = time.perf_counter_ns()
        self.epoch_wall = time.time()
        self.lock = threading.Lock()
        self.reader = threading.Lock()

    def now_ns(self) -> int:
        return time.perf_counter_ns() - self.epoch_ns

    def put(
        self,
        kind: int,
        job: int,
        addr: int | None,
        lane: str | None,
        unit: str | None,
        queued_s: float | None,
        service_s: float | None,
        aux: int,
        status: int,
    ) -> None:
        """Write one record now; lane and unit by name (None: none)."""
        with self.lock:
            t = time.perf_counter_ns() - self.epoch_ns
            lid = 0 if lane is None else self.names.id("lane", lane, t)
            uid = 0 if unit is None else self.names.id("unit", unit, t)
            if self.head - self.tail >= self.capacity:
                self.dropped += 1
                return
            rec = pack(t, kind, job, addr, lid, uid, queued_s, service_s, aux, status)
            if rec[38] & (QUEUED_SAT | SERVICE_SAT):
                self.saturated += 1
            at = (self.head & (self.capacity - 1)) * SIZE
            self.buf[at : at + SIZE] = rec
            self.head += 1
            self.written += 1

    def drain(self, max_records: int = 1 << 30) -> bytes:
        """The records written since the last drain (up to ``max_records``), oldest first.

        The copy runs outside the writers' lock: a writer never touches ``[tail, head)``, and
        ``tail`` moves only after the copy (one reader at a time).
        """
        with self.reader:
            with self.lock:
                n = min(self.head - self.tail, max_records)
                start = self.tail & (self.capacity - 1)
            first = min(n, self.capacity - start)
            out = bytes(self.buf[start * SIZE : (start + first) * SIZE])
            if first < n:
                out += bytes(self.buf[: (n - first) * SIZE])
            with self.lock:
                self.tail += n
            return out

    def name_id(self, kind: str, name: str) -> int:
        """The id of a lane or unit name (allocated now when new)."""
        with self.lock:
            return self.names.id(kind, name, self.now_ns())

    def leave(self, kind: str, name: str) -> None:
        """Mark the time a lane or unit left (its id stays)."""
        with self.lock:
            self.names.leave(kind, name, self.now_ns())

    def name_rows(self) -> list[tuple]:
        """Every ``(kind, id, name, appeared_ns, left_ns)``."""
        with self.lock:
            return [tuple(r) for r in self.names.rows]

    def counts(self) -> dict:
        with self.lock:
            return {
                "written": self.written,
                "dropped": self.dropped,
                "saturated": self.saturated,
                "pending": self.head - self.tail,
                "capacity": self.capacity,
            }
