"""The telemetry run directory and what it stores (spec/08 §8.5).

```
<dir>/
  machine.json      the run: start wall time, core, inspector url, tiers, the writer's knobs
  static.kv         programs (graphs, statement table), name table, series names, machine spec
  events.kv         trace/<t_ms>/<job>: kept job traces
  raw/<t>.cv        every record (column "rec", vec:u8:40), one file per raw segment
  r1s/<t>.cv        1 s rollups, one file per hour
  r1m/<t>.cv        1 min rollups, one file per day
  r1h.cv            1 h rollups, the whole run
  metrics/<t>.cv    series samples (t, sid, v), one file per day
```

A rollup row is one (bucket, entity, metric): its count, and for a valued metric the sum,
minimum, maximum and log histogram (``plugins/common/hist.py``) of the values. An entity is
``kind << 56 | id``: a statement by code address, a lane or unit by name-table id, an entry by
its code address.
"""

import numpy as np

from togeari.core.contracts import telemetry as T

REC = np.dtype(
    [
        ("t_ns", "<u8"),
        ("job", "<u8"),
        ("addr", "<u4"),
        ("queued_us", "<u4"),
        ("service_us", "<u4"),
        ("aux", "<u4"),
        ("lane", "<u2"),
        ("unit", "<u2"),
        ("kind", "u1"),
        ("status", "u1"),
        ("flags", "<u2"),
    ]
)
assert REC.itemsize == T.SIZE

# Entity kinds
MACHINE, NODE, LANE, UNIT, ENTRY = 0, 1, 2, 3, 4
ENTITY_KINDS = {"machine": MACHINE, "node": NODE, "lane": LANE, "unit": UNIT, "entry": ENTRY}
ID_MASK = (1 << 56) - 1


def entity(kind: int, ident: int) -> int:
    return (kind << 56) | ident


def entity_kind(e: int) -> int:
    return e >> 56


def entity_id(e: int) -> int:
    return e & ID_MASK


# Metrics: valued ones carry values (µs, or items for "batch"); the rest only count
WAIT, QUEUE, SERVICE, CALLS, ERRORS, BATCH, JOB, ADMITTED, FAILED, FAULTS, CANCELLED = range(1, 12)
METRICS = {
    "wait": WAIT,  # statement: fetch to issue
    "queue": QUEUE,  # statement, lane, unit: enqueued to sent
    "service": SERVICE,  # statement, lane, unit: sent to result (inline: run time)
    "calls": CALLS,  # statement: enqueued, inline and store-hit calls
    "errors": ERRORS,  # statement, unit: failed attempts (error results, raising inline runs)
    "batch": BATCH,  # lane, unit: items per submission
    "job": JOB,  # entry: job wall time
    "admitted": ADMITTED,  # entry
    "failed": FAILED,  # entry: failed or cancelled jobs
    "faults": FAULTS,  # statement: faults raised (a failure the job sees)
    "cancelled": CANCELLED,  # statement, unit: cancelled results
}
VALUED = {WAIT, QUEUE, SERVICE, BATCH, JOB}

# Files
MACHINE_FILE = "machine.json"
STATIC_FILE = "static.kv"
EVENTS_FILE = "events.kv"

# Rollup tiers: (name, bucket seconds, seconds per file; None: one file)
TIERS = (("r1s", 1, 3600), ("r1m", 60, 86400), ("r1h", 3600, None))
ROLLUP_COLUMNS = {
    "t": "i64",
    "ent": "u64",
    "met": "u8",
    "n": "u64",
    "sum": "f64",
    "lo": "f64",
    "hi": "f64",
}
RAW_COLUMNS = {"rec": f"vec:u8:{T.SIZE}"}
METRIC_COLUMNS = {"t": "f64", "sid": "u32", "v": "f64"}
METRICS_SEGMENT_S = 86400


def hist_dtype(tier: str) -> str:
    """The histogram column: sparse bytes at 1 s (few bins per row), dense above."""
    return "bytes" if tier == "r1s" else "vec:u32:64"
