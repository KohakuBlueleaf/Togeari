"""The telemetry record: fixed 40 bytes per hot transition of the machine (spec/08 §8.5).

The dispatcher (and a lane's pump) writes one record per admit, end, enqueue, submit, result,
fault, park, resume, inline call and store hit into a ring (``togeari.core.observe.ring``; the
native core's ``togeari._rs.TelemetryRing`` writes the same bytes).

Record (little-endian, 40 bytes):

| offset | field | type | meaning |
|---|---|---|---|
| 0 | ``t_ns`` | u64 | ns since the ring's epoch (``epoch_wall`` is its wall time) |
| 8 | ``job`` | u64 | job id |
| 16 | ``addr`` | u32 | code address (the entry's for admit and end); ``NO_ADDR`` none |
| 20 | ``queued_us`` | u32 | per kind, below |
| 24 | ``service_us`` | u32 | per kind, below |
| 28 | ``aux`` | u32 | per kind, below |
| 32 | ``lane`` | u16 | lane id (0: none) |
| 34 | ``unit`` | u16 | unit id (0: none) |
| 36 | ``kind`` | u8 | below |
| 37 | ``status`` | u8 | per kind, below |
| 38 | ``flags`` | u16 | ``QUEUED`` / ``SERVICE``: the field holds a value; ``*_SAT``: clamped |

Lane and unit ids come from the ring's name table: allocated at first use, never reused; a
name keeps its id for the ring's life.
"""

import struct

# Kinds, and what each puts in (queued_us, service_us, aux, status)
ADMIT = 1  # -, -, -, -
END = 2  # -, job seconds, -, 0 ok / 1 failed / 2 cancelled
ENQUEUE = 3  # fetch to issue, -, -, -
SUBMIT = 4  # -, -, items, -
RESULT = 5  # enqueued to sent, sent to result, -, RESULT_STATUS
FAULT = 6  # -, -, squashed so far, -
PARK = 7  # -, -, strand, -
RESUME = 8  # -, -, strand, -
INLINE = 9  # fetch to issue, run time, -, 0 ok / 1 error
HIT = 10  # fetch to issue, -, -, -
KINDS = {
    "admit": ADMIT,
    "end": END,
    "enqueue": ENQUEUE,
    "submit": SUBMIT,
    "result": RESULT,
    "fault": FAULT,
    "park": PARK,
    "resume": RESUME,
    "inline": INLINE,
    "hit": HIT,
}
NAMES = {code: name for name, code in KINDS.items()}

# Result status by result class name; any other class is RESULT_OTHER
RESULT_STATUS = {"Ok": 0, "ItemError": 1, "UnitError": 2, "Cancelled": 3}
RESULT_OTHER = 4

# Flags
QUEUED, SERVICE, QUEUED_SAT, SERVICE_SAT = 1, 2, 4, 8

NO_ADDR = 0xFFFFFFFF
U32 = 0xFFFFFFFF
REC = struct.Struct("<QQIIIIHHBBH")
SIZE = REC.size
DEFAULT_RECORDS = 1 << 19


def micros(seconds: float | None, valid: int, saturated: int) -> tuple[int, int]:
    """(µs as a u32, flags): 0 and no flag for None; clamped at u32 max with ``saturated``."""
    if seconds is None:
        return 0, 0
    us = seconds * 1e6
    if us != us or us <= -1.0:
        return 0, valid
    if us > U32:
        return U32, valid | saturated
    return int(us), valid


def pack(
    t_ns: int,
    kind: int,
    job: int,
    addr: int | None,
    lane: int,
    unit: int,
    queued_s: float | None,
    service_s: float | None,
    aux: int,
    status: int,
) -> bytes:
    """One record's bytes (the layout above)."""
    q, qf = micros(queued_s, QUEUED, QUEUED_SAT)
    s, sf = micros(service_s, SERVICE, SERVICE_SAT)
    a = NO_ADDR if addr is None or addr < 0 or addr > U32 else addr
    return REC.pack(t_ns, job, a, q, s, aux & U32, lane, unit, kind, status, qf | sf)
