"""Prints the digests of one trace as JSON: ``python trace_child.py <generator seed> <trace seed>``.

Run in a fresh process by the trace checks, so a trace is compared across processes.
"""

import json
import sys

from kohakunode.testing import entry_program
from trace_machines import generated

from togeari.testing.trace import records_digest, run_trace


def digests(gen_seed: int, seed: int) -> dict:
    p = entry_program(gen_seed, jobs=16)
    factory = generated(p.text, p.entry, item_error_rate=0.05, outages=[(0.004, 0.02)])
    t = run_trace(factory, p.inputs, seed=seed)
    return {"log": t.log.digest(), "records": records_digest(t.records), "n": len(t.log)}


if __name__ == "__main__":
    print(json.dumps(digests(int(sys.argv[1]), int(sys.argv[2]))))
