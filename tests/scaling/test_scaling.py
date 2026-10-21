"""Cost per job, per `each` item and per loop iteration at 4N stays within a constant factor of
the cost at N (a cost that grows with the queue, the pool, the strand count, the order keys or
the versions of a value fails here)."""

import importlib.util
import time
from pathlib import Path

import pytest
from kohakunode import NodeLibrary

from togeari import Inline, Machine

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "benches" / "scaling.py"
_SPEC = importlib.util.spec_from_file_location("togeari_bench_scaling", _PATH)
scaling = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(scaling)

# Wall-clock ratios: run alone, never beside other workers
pytestmark = pytest.mark.serial

# Allowed growth of the per-unit cost from N to 4N (linear-in-N costs give about 4; the sizes
# are where such costs dominate the constant per-unit work)
FACTOR = 2.0

LIB = NodeLibrary("scaling_loops")


@LIB.node()
def inc(x: int) -> int:
    return x + 1


@LIB.node()
def mklist(x: int) -> list:
    return [x]


@LIB.node()
def ident(v):
    return v


@LIB.node(mutates=["xs"], outputs=["n"])
def push(xs: list) -> int:
    xs.append(0)
    return len(xs)


# A loop written with `jump`; NODE reads the list (ident) or changes it in place (push)
LOOP = """
@def (k: int)main(xs, x):
    x = 0
    (x)mklist(xs)
    ()jump(`top`)
    top:
        (xs)NODE(m)
        (x)inc(x)
        (x, k)less(more)
        (more)branch(`again`, `done`)
        again:
            ()jump(`top`)
        done:
"""


def per_iteration_us(node: str, k: int) -> float:
    """Best of three: wall time of one job running ``k`` iterations, per iteration."""
    bind = {"mklist": Inline(), "inc": Inline(), node: Inline(), "kir.std.*": Inline()}
    m = Machine(program=LOOP.replace("NODE", node), entry="main", libraries=[LIB], bind=bind)
    best = float("inf")
    try:
        for _ in range(3):
            t0 = time.perf_counter()
            rec = m.submit({"k": k}).result(120)
            best = min(best, time.perf_counter() - t0)
            assert rec.ok and rec.outputs["x"] == k, rec.failure
    finally:
        m.stop()
    return best / k * 1e6


@pytest.mark.parametrize(
    "measure, n",
    [(scaling.per_job_us, 2000), (scaling.per_item_us, 1000)],
    ids=["per_job_resident", "per_each_item"],
)
def test_cost_per_unit_is_flat_from_n_to_4n(measure, n):
    small = measure(n)
    large = measure(4 * n)
    assert large <= FACTOR * small, f"{small:.1f} us at {n}, {large:.1f} us at {4 * n}"


@pytest.mark.parametrize("node", ["ident", "push"], ids=["jump_loop", "mutates_loop"])
def test_cost_per_loop_iteration_is_flat_from_n_to_4n(node):
    n = 500
    small = per_iteration_us(node, n)
    large = per_iteration_us(node, 4 * n)
    assert large <= FACTOR * small, f"{small:.1f} us at {n}, {large:.1f} us at {4 * n}"
