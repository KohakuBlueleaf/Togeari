"""Machines for the trace checks: the generator's library on scripted lanes, and a tiny one."""

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
    **knobs,
):
    """A factory of machines running every generator node on a scripted lane of ``n_units``."""

    def make() -> Machine:
        lanes = {}
        for kind in ("call", "batch", "stream"):
            units = [
                ScriptedEngine(
                    kind,
                    name=f"{kind}{i}",
                    latency_ms=latency_ms,
                    capacity=CAPACITY[kind],
                    item_error_rate=item_error_rate,
                    outages=outages if i == 0 else None,
                )
                for i in range(n_units)
            ]
            former = SizeOrWait(8, 1) if kind == "batch" else None
            lanes[kind] = Lane(units, former=former, name=kind)
        bind = {name: Use(lanes[k]) for name, k in LANE_OF.items()}
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
