"""Study sizes: ``smoke`` (tiny, for verification) and ``full`` (the study).

Phase names are ``<work>+<load>``: ``work`` is how the engine under test is fed
(``direct``: a feeder thread submits to the unit; ``lane``: the machine's
dispatcher runs one job per call through a lane), ``load`` is the chain load on
the dispatcher (``ceiling``: as many chain jobs as it can run; ``paced``:
``paced_jobs_per_s`` chain jobs per second). Either part may stand alone.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Shape:
    """A synthetic torch module: ``depth`` blocks of ``width``, a head of ``out``."""

    name: str
    width: int
    depth: int
    out: int
    max_batch: int


@dataclass(frozen=True)
class Scale:
    name: str

    # Every phase: warm-up, then the measured window
    warmup_s: float
    phase_s: float
    # Children: one per "config" or one per "measure"; wall-clock limit per config
    isolate: str
    child_timeout_s: float
    # Parent-side device sampling interval (nvidia-smi)
    gpu_sample_s: float

    # Dispatcher chain load: calls per job, jobs in flight at the ceiling, paced rate
    chain_len: int
    chain_inflight: int
    paced_jobs_per_s: float

    # M1: payload sizes and kinds; messages per (link, size) = budget / size, clamped,
    # repeated until a timed block lasts ``m1_min_s`` (CPU clocks tick at 15.6 ms on Windows)
    m1_sizes: tuple[int, ...]
    m1_kinds: tuple[str, ...]
    m1_links: tuple[str, ...]
    m1_budget_bytes: int
    m1_min_n: int
    m1_max_n: int
    m1_min_s: float
    m1_window: int

    # Torch: shapes, VRAM budget for the whole study process group
    shapes: tuple[Shape, ...]
    vram_total_gb: float
    cuda_context_gb: float

    # M2: dispatcher impact of torch work; lane phases run once per batch-former wait
    m2_former_waits_ms: tuple[float, ...]
    m2_shapes: tuple[str, ...]
    m2_isolations: tuple[str, ...]
    m2_models: tuple[str, ...]
    m2_phases: tuple[str, ...]
    m2_base_phases: tuple[str, ...]

    # M3: GPU sharing between two models
    m3_models: tuple[str, str]
    m3_layouts: tuple[str, ...]

    # M4: Python work
    m4_workers: int
    m4_fns: tuple[str, ...]
    m4_modes: tuple[str, ...]
    m4_phases: tuple[str, ...]
    spin_n: int
    hash_bytes: int
    blob_bytes: int

    # M5: HTTP client; None is no rate limit
    m5_rates: tuple[float | None, ...]
    m5_pad_bytes: int
    m5_inflight: int
    m5_phases: tuple[str, ...]

    def shape(self, name: str) -> Shape:
        return next(s for s in self.shapes if s.name == name)

    def cap_gb(self, processes: int) -> float:
        """Allocator cap per CUDA process so ``processes`` of them stay within the VRAM budget."""
        return (self.vram_total_gb - processes * self.cuda_context_gb) / processes


ENGINE_PHASES = (
    "direct",
    "direct+ceiling",
    "direct+paced",
    "lane",
    "lane+ceiling",
    "lane+paced",
)
M3_LAYOUTS = (
    "thread/co_resident",
    "thread/swappable",
    "thread/priority_streams",
    "thread/two-engines",
    "process/co_resident",
    "process/two-engines",
)

SMOKE = Scale(
    name="smoke",
    warmup_s=0.2,
    phase_s=0.5,
    isolate="measure",
    child_timeout_s=120.0,
    gpu_sample_s=1.0,
    chain_len=20,
    chain_inflight=128,
    paced_jobs_per_s=50.0,
    m1_sizes=(1 << 10, 100 << 10, 2 << 20, 8 << 20),
    m1_kinds=("ndarray",),
    m1_links=("thread", "pipe", "shm-copy", "shm-map", "http"),
    m1_budget_bytes=32 << 20,
    m1_min_n=3,
    m1_max_n=20,
    m1_min_s=0.02,
    m1_window=4,
    shapes=(
        Shape("deep", width=64, depth=4, out=8, max_batch=8),
        Shape("wide", width=256, depth=2, out=8, max_batch=16),
    ),
    vram_total_gb=16.0,
    cuda_context_gb=0.75,
    m2_former_waits_ms=(2.0, 1000.0),
    m2_shapes=("deep",),
    m2_isolations=("thread", "process"),
    m2_models=("eager", "graph"),
    m2_phases=ENGINE_PHASES,
    m2_base_phases=("ceiling", "paced"),
    m3_models=("deep", "wide"),
    m3_layouts=("thread/co_resident", "thread/swappable", "process/two-engines"),
    m4_workers=2,
    m4_fns=("spin", "digest", "checksum"),
    m4_modes=("thread", "process/pickle", "process/shm"),
    m4_phases=("direct", "direct+ceiling", "lane"),
    spin_n=2_000,
    hash_bytes=256 << 10,
    blob_bytes=256 << 10,
    m5_rates=(200.0, None),
    m5_pad_bytes=256,
    m5_inflight=16,
    m5_phases=("direct", "direct+ceiling", "lane"),
)

FULL = Scale(
    name="full",
    warmup_s=1.0,
    phase_s=2.5,
    isolate="config",
    child_timeout_s=600.0,
    gpu_sample_s=1.0,
    chain_len=20,
    chain_inflight=256,
    paced_jobs_per_s=100.0,
    m1_sizes=(1 << 10, 100 << 10, 2 << 20, 8 << 20),
    m1_kinds=("ndarray",),
    m1_links=("thread", "pipe", "shm-copy", "shm-map", "http"),
    m1_budget_bytes=512 << 20,
    m1_min_n=64,
    m1_max_n=2000,
    m1_min_s=0.5,
    m1_window=8,
    shapes=(
        Shape("deep", width=1024, depth=64, out=16, max_batch=64),
        Shape("wide", width=8192, depth=8, out=16, max_batch=512),
    ),
    vram_total_gb=16.0,
    cuda_context_gb=0.75,
    m2_former_waits_ms=(2.0, 1000.0),
    m2_shapes=("deep", "wide"),
    m2_isolations=("thread", "process"),
    m2_models=("eager", "graph"),
    m2_phases=ENGINE_PHASES,
    m2_base_phases=("ceiling", "paced"),
    m3_models=("deep", "wide"),
    m3_layouts=M3_LAYOUTS,
    m4_workers=8,
    m4_fns=("spin", "digest", "checksum"),
    m4_modes=("thread", "process/pickle", "process/shm"),
    m4_phases=ENGINE_PHASES,
    spin_n=40_000,
    hash_bytes=4 << 20,
    blob_bytes=2 << 20,
    m5_rates=(100.0, 500.0, 2000.0, None),
    m5_pad_bytes=1024,
    m5_inflight=64,
    m5_phases=ENGINE_PHASES,
)

SCALES = {s.name: s for s in (SMOKE, FULL)}
