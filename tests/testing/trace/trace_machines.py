"""Machines for the trace checks: the generator's library on scripted lanes, and a tiny one."""

import types

from kohakunode import NodeLibrary
from kohakunode.testing import LIB

from togeari import Inline, Lane, Machine, Use
from togeari.policies import Fixed, SizeOrWait
from togeari.testing.trace import ScriptedEngine

CAPACITY = {"call": 8, "batch": 2, "stream": 256}
LANE_OF = {"inc": "call", "mix": "batch", "flaky": "stream", "small": "call"}
LANE_OF |= {"total": "stream", "pick": "batch", "emit": "call", "bump": "call"}

TINY = NodeLibrary("tiny")


@TINY.node()
def inc(x: int) -> int:
    return x + 1


TWO = "@def (x: int)main(y):\n    (x)inc(a)\n    (a)inc(y)\n"


def generated(
    program: str,
    entry: str,
    n_units: int = 2,
    latency_ms: float = 1.0,
    item_error_rate: float = 0.0,
    outages: list | None = None,
    lane_kw: dict | None = None,
    per_kind: dict | None = None,
    lane_cls: type = Lane,
    use_kw: dict | None = None,
    engine_cls: type = ScriptedEngine,
    **knobs,
):
    """A factory of machines running every generator node on a scripted lane of ``n_units``.

    ``lane_kw`` applies to every lane and ``per_kind[kind]`` (keys ``lane``, ``engine``) to one;
    a callable ``former``, ``select`` or ``retry`` is called per lane, so lanes share no policy.
    Units are ``engine_cls`` (a ``ScriptedEngine``).
    """

    def make() -> Machine:
        lanes = {}
        for kind in ("call", "batch", "stream"):
            mine = (per_kind or {}).get(kind, {})
            engine = mine.get("engine", {})
            units = [
                engine_cls(
                    kind,
                    name=f"{kind}{i}",
                    latency_ms=latency_ms,
                    capacity=engine.get("capacity", CAPACITY[kind]),
                    max_batch=engine.get("max_batch", 16),
                    item_error_rate=item_error_rate,
                    outages=outages if i == 0 else None,
                )
                for i in range(n_units)
            ]
            kw = {"former": SizeOrWait(8, 1) if kind == "batch" else None}
            kw |= {**(lane_kw or {}), **mine.get("lane", {})}
            for k in ("former", "select", "retry"):
                if isinstance(kw.get(k), type | types.FunctionType):
                    kw[k] = kw[k]()
            if kind != "batch":
                kw["former"] = None
            lanes[kind] = lane_cls(units, name=kind, **kw)
        bind = {name: Use(lanes[k], **(use_kw or {})) for name, k in LANE_OF.items()}
        bind["kir.std.*"] = Inline()
        bind["f*"] = Use(lanes["call"])
        knobs.setdefault("admission", Fixed(8))
        knobs.setdefault("record", 1.0)
        return Machine(program=program, entry=entry, libraries=[LIB], bind=bind, **knobs)

    return make


def tiny(latency_ms: float = 1.0, **knobs):
    """A factory of machines running ``TWO`` with ``inc`` on one scripted call unit."""

    def make() -> Machine:
        unit = ScriptedEngine("call", name="u", latency_ms=latency_ms, capacity=4)
        bind = {"inc": Use(Lane([unit], name="calls"))}
        return Machine(program=TWO, entry="main", libraries=[TINY], bind=bind, **knobs)

    return make
