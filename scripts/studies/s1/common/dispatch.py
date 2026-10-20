"""The machine under test: a chain entry that loads the dispatcher, plus an optional work entry.

``chain`` is ``chain_len`` calls of ``inc`` on a zero-latency simulated call
lane (the dispatcher ceiling shape of ``scripts/benches/dispatcher_ceiling.py``).
``work`` is one call of the node under test, bound by the measurement. The
``s1_probe`` query copies the dispatcher's counters on its own thread, with
that thread's CPU time.
"""

import copy
import time
from typing import Any

from kohakunode import NodeLibrary

from togeari import Lane, Machine, Use
from togeari.policies import Fixed
from togeari.testing import SimCall

lib = NodeLibrary("s1")


@lib.node()
def inc(x: int) -> int:
    return x + 1


def chain_program(n: int) -> str:
    """``@def (x)chain(y)``: ``n`` calls of ``inc`` in sequence."""
    body = "".join(f"    ({'x' if i == 0 else f'v{i - 1}'})inc(v{i})\n" for i in range(n))
    return f"@def (x)chain(y):\n{body}    y = v{n - 1}\n"


def work_program(node: str, inputs: tuple[str, ...]) -> str:
    """``@def (<inputs>)work(y)``: one call of ``node``."""
    args = ", ".join(inputs)
    return f"@def ({args})work(y):\n    ({args}){node}(y)\n"


def probe(m: Any) -> dict:
    """The dispatcher's counters and thread CPU time, read on the dispatcher thread."""
    return {
        "stats": copy.deepcopy(m.dispatcher.stats),
        "thread_cpu_s": time.thread_time(),
        "wall": time.perf_counter(),
        "jobs_live": len(m.dispatcher.jobs),
    }


def build(
    chain_len: int,
    work: tuple[str, tuple[str, ...]] | None = None,
    libraries: tuple = (),
    bind: dict | None = None,
    window: int = 256,
) -> Machine:
    """A machine with the ``chain`` entry, and ``work`` (node, inputs) when given."""
    sim = Lane([SimCall(name="sim", latency_ms=0.0, capacity=64)], name="chain")
    programs = {chain_program(chain_len): ["chain"]}
    if work is not None:
        programs[work_program(*work)] = ["work"]
    m = Machine(
        programs=programs,
        entry="chain",
        libraries=[lib, *libraries],
        bind={"inc": Use(sim), **(bind or {})},
        admission=Fixed(window),
        source_buffer=64,
    )
    m.queries["s1_probe"] = probe
    return m


def snapshot(m: Any) -> dict:
    return m.inspect("s1_probe").result(timeout=60.0)


def window_stats(a: dict, b: dict) -> dict:
    """Dispatcher numbers between two probes: calls/s, busy fraction, phases, cycle histogram."""
    wall = b["wall"] - a["wall"]
    sa, sb = a["stats"], b["stats"]
    calls = sb["calls"] - sa["calls"]
    jobs = (sb["jobs_ok"] + sb["jobs_failed"]) - (sa["jobs_ok"] + sa["jobs_failed"])
    busy = sb["busy_s"] - sa["busy_s"]
    cpu = b["thread_cpu_s"] - a["thread_cpu_s"]
    phases = {k: v - sa["phase_s"].get(k, 0.0) for k, v in sb["phase_s"].items()}
    hist = {
        f"<{2**k}us": v - sa["cycle_hist"].get(k, 0)
        for k, v in sorted(sb["cycle_hist"].items())
        if v - sa["cycle_hist"].get(k, 0)
    }
    cycles = sb["cycles"] - sa["cycles"]
    return {
        "wall_s": wall,
        "calls": calls,
        "calls_per_s": calls / wall if wall > 0 else 0.0,
        "jobs": jobs,
        "jobs_per_s": jobs / wall if wall > 0 else 0.0,
        "jobs_failed": sb["jobs_failed"] - sa["jobs_failed"],
        "busy_s": busy,
        "busy_fraction": busy / wall if wall > 0 else 0.0,
        "thread_cpu_s": cpu,
        "thread_cpu_fraction": cpu / wall if wall > 0 else 0.0,
        "cycles": cycles,
        "cycle_mean_us": busy / cycles * 1e6 if cycles else None,
        "cycle_max_s_so_far": sb["cycle_max_s"],
        "phase_s": phases,
        "cycle_hist": hist,
        "jobs_live_end": b["jobs_live"],
    }
