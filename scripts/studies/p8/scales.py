"""Study sizes: ``small`` checks the scripts in seconds; ``full`` answers the questions.

A load is ``(name, chain jobs per second)``: 0 is no chain load, None is as many chain jobs as
the dispatcher runs (its ceiling). Each chain job is ``chain_len`` zero-latency engine calls.
"""

from dataclasses import dataclass

from s1.scales import FULL as S1_FULL
from s1.scales import SMOKE as S1_SMOKE
from s1.scales import Shape


@dataclass(frozen=True)
class Scale:
    name: str
    # Every phase: warm-up, then the measured window
    warmup_s: float
    phase_s: float
    child_timeout_s: float
    # Dispatcher load: calls per chain job, chain jobs in flight, the loads in run order
    chain_len: int
    chain_inflight: int
    loads: tuple[tuple[str, float | None], ...]
    # Engine work: kinds, isolations, the torch network, the Python workers and loop size
    works: tuple[str, ...]
    isolations: tuple[str, ...]
    shape: Shape
    vram_cap_gb: float
    py_workers: int
    spin_n: int


LOADS = (("idle", 0.0), ("100/s", 100.0), ("300/s", 300.0), ("1000/s", 1000.0), ("ceiling", None))

SCALES = {
    "small": Scale(
        name="small",
        warmup_s=0.2,
        phase_s=0.5,
        child_timeout_s=300.0,
        chain_len=20,
        chain_inflight=128,
        loads=(("idle", 0.0), ("100/s", 100.0), ("ceiling", None)),
        works=("torch", "python"),
        isolations=("thread", "process"),
        shape=S1_SMOKE.shape("deep"),
        vram_cap_gb=2.0,
        py_workers=2,
        spin_n=2_000,
    ),
    "full": Scale(
        name="full",
        warmup_s=1.0,
        phase_s=2.5,
        child_timeout_s=900.0,
        chain_len=20,
        chain_inflight=256,
        loads=LOADS,
        works=("torch", "python"),
        isolations=("thread", "process"),
        shape=S1_FULL.shape("deep"),
        vram_cap_gb=4.0,
        py_workers=8,
        spin_n=40_000,
    ),
}
