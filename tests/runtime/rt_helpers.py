"""Shared nodes, programs and helpers of the runtime checks."""

import threading
import time

from kohakunode import NodeLibrary

from togeari import BasePlugin, Inline, Lane, Machine, Use
from togeari.testing import SimCall

LIB = NodeLibrary("rt")
CALLS: list = []


@LIB.node()
def inc(x: int) -> int:
    return x + 1


@LIB.node()
def nothing(x: int) -> object:
    CALLS.append(x)
    return None


@LIB.node()
def mklist(x: int) -> list:
    return [x]


@LIB.node(mutates=["xs"], outputs=["n"])
def push(xs: list) -> int:
    xs.append(99)
    return len(xs)


@LIB.node(mutates=["xs"], outputs=["n"])
def zero(xs: list) -> int:
    xs[0] = 0
    return len(xs)


@LIB.node()
def ident(v):
    return v


# `block` sets ENTERED, then waits for RELEASE (10 s at most)
ENTERED = threading.Event()
RELEASE = threading.Event()


@LIB.node()
def block(x: int) -> int:
    ENTERED.set()
    RELEASE.wait(10)
    return x


@LIB.node()
def lock(x: int) -> object:
    return threading.Lock()


SLOW = [False]


@LIB.node()
def slow_inline(x: int) -> int:
    if SLOW[0]:
        time.sleep(0.0002)
    return x


@LIB.node()
def isbig(x: int) -> bool:
    return x > 5


INC = "@def (x: int)main(y):\n    (x)inc(y)\n"
TWO = "@def (x: int)main(y):\n    (x)inc(a)\n    (a)inc(y)\n"


def sim_lane(
    name: str = "s", latency_ms: float = 1.0, capacity: int = 8, tail: float = 1.0
) -> Lane:
    unit = SimCall(name=name, latency_ms=latency_ms, capacity=capacity, tail=tail)
    return Lane([unit], name=f"{name}-lane")


def machine(program: str = INC, bind: dict | None = None, **kw) -> Machine:
    bind = bind if bind is not None else {"inc": Use(sim_lane())}
    bind.setdefault("kir.std.*", Inline())
    return Machine(program=program, entry="main", libraries=[LIB], bind=bind, **kw)


def wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def in_thread(fn, timeout: float):
    """(finished, result or exception) of ``fn()`` on a daemon thread, waiting ``timeout``."""
    out: dict = {}

    def go():
        try:
            out["v"] = fn()
        except BaseException as exc:  # noqa: BLE001
            out["v"] = exc

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), out.get("v")


def plugin(name: str = "hooks", base: type = BasePlugin, **members):
    """A plugin instance whose hooks are the given functions (called without ``self``)."""
    body = {k: staticmethod(v) if callable(v) else v for k, v in members.items()}
    body["name"] = name
    return type(f"P_{name}", (base,), body)()
