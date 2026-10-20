"""M1: handoff latency and throughput per transport, per payload kind and size.

One config per link (thread queue, pipe + pickle, shared memory copied or
mapped, HTTP localhost). Per payload size: one-way latency (send start to
decoded at the peer), echo round trip, and a windowed stream for throughput,
each with the sender's and the peer's CPU per message.
"""

import pickle
import time
from collections.abc import Callable

from s1 import links
from s1.common.stats import percentiles, scaled
from s1.scales import Scale
from s1.workloads import payloads

NAME = "m1"
TITLE = "Handoff latency and throughput per transport"


def plan(scale: Scale) -> list[dict]:
    return [{"name": link, "link": link} for link in scale.m1_links]


def count(scale: Scale, size: int) -> int:
    """Messages per mode for one payload size."""
    return max(scale.m1_min_n, min(scale.m1_max_n, scale.m1_budget_bytes // size))


def _timed(link: object, call: Callable[[], float], n: int, min_s: float) -> dict:
    """``call()`` at least ``n`` times and ``min_s`` seconds: its values, wall and CPU seconds."""
    peer0 = link.peer_cpu()
    cpu0 = time.process_time()
    t0 = time.perf_counter()
    values = []
    while len(values) < n or time.perf_counter() - t0 < min_s:
        values.append(call())
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    peer1 = link.peer_cpu()
    return {
        "values": values,
        "wall_s": wall,
        "sender_cpu_s": cpu,
        "peer_cpu_s": None if peer0 is None else peer1 - peer0,
    }


def _per_msg(x: float | None, n: int) -> float | None:
    return None if x is None else x / n * 1e6


def _mode(timed: dict, n: int) -> dict:
    """CPU per message and wall time of a timed block of ``n`` messages."""
    return {
        "n": n,
        "wall_s": timed["wall_s"],
        "sender_cpu_us_per_msg": _per_msg(timed["sender_cpu_s"], n),
        "peer_cpu_us_per_msg": _per_msg(timed["peer_cpu_s"], n),
    }


def one_size(link: object, kind: str, size: int, scale: Scale) -> dict:
    obj = payloads.make(kind, size)
    n = count(scale, size)
    wire = len(pickle.dumps(obj, protocol=5))

    # Warm-up: connections, imports, allocator
    for _ in range(2):
        link.oneway(obj)
        link.echo(obj)

    oneway = _timed(link, lambda: link.oneway(obj), n, scale.m1_min_s)
    echo = _timed(link, lambda: link.echo(obj), n, scale.m1_min_s)

    # Stream: rounds of ``n`` messages, a window of them unacknowledged
    stream = _timed(link, lambda: link.stream([obj] * n, scale.m1_window), 1, scale.m1_min_s)
    sent = n * len(stream["values"])
    seconds = sum(stream["values"])

    return {
        "kind": kind,
        "size": size,
        "pickled_bytes": wire,
        "oneway": {
            "latency_us": scaled(percentiles(oneway["values"]), 1e6),
            **_mode(oneway, len(oneway["values"])),
        },
        "echo": {
            "rtt_us": scaled(percentiles(echo["values"]), 1e6),
            **_mode(echo, len(echo["values"])),
        },
        "stream": {
            "window": scale.m1_window,
            "rounds": len(stream["values"]),
            "seconds": seconds,
            "msgs_per_s": sent / seconds if seconds > 0 else 0.0,
            "mb_per_s": sent * size / seconds / 2**20 if seconds > 0 else 0.0,
            **_mode(stream, sent),
        },
        "error": None,
    }


def run(cfg: dict, scale: Scale) -> dict:
    link = links.make(cfg["link"])
    link.open()
    rows = []
    try:
        for kind in scale.m1_kinds:
            for size in scale.m1_sizes:
                try:
                    rows.append(one_size(link, kind, size, scale))
                except Exception as exc:  # noqa: BLE001
                    rows.append({"kind": kind, "size": size, "error": repr(exc)})
    finally:
        link.close()
    return {"link": cfg["link"], "sizes": rows}
