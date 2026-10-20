"""Generated programs on simulated engines must give the reference interpreter's results
(spec/07 §7.2): 32 jobs per program under every configuration, checked by re-execution."""

import os
from collections import Counter

import pytest
from kohakunode.testing import entry_program

from conftest import SELECTORS, bounded, build, conform_all, keyed, pure, run_jobs
from togeari import BasePlugin, Retry
from togeari.stores import MemoryStore

SEEDS = range(int(os.environ.get("TOGEARI_SEEDS", "20")))
JOBS = 32
BOUND_S = 120.0

# Faults: about one call in a hundred fails on its unit; unit i of every lane but the last goes
# down at its (2 + i)-th submit and is back at its first health poll
FAULTS = dict(item_error_rate=0.01, outage=(2, 1), retry=Retry(max=5, backoff_ms=1))

CONFIGS = {
    "sequential": dict(n_units=1, window=1, latency_ms=0.2),
    "w64": dict(n_units=3, window=64, latency_ms=0.5),
    "w256": dict(n_units=3, window=256, latency_ms=0.5),
    "faults": dict(n_units=3, window=64, latency_ms=0.5, **FAULTS),
    "early": dict(n_units=3, window=64, latency_ms=0.5, forming="early"),
    "early_faults": dict(n_units=3, window=256, latency_ms=0.5, forming="early", **FAULTS),
    "selectors": dict(n_units=3, window=64, latency_ms=0.5, select="rotate"),
    "no_runahead": dict(n_units=2, window=16, latency_ms=0.5, run_ahead=False),
    "copy_always": dict(n_units=2, window=64, latency_ms=0.5, mutates="copy-always"),
    "mutates_wait": dict(n_units=2, window=64, latency_ms=0.5, mutates="wait"),
    "content_store": dict(n_units=2, window=64, latency_ms=0.5, store_mode="content"),
    "plugged": dict(n_units=3, window=64, latency_ms=1.0, plugged=True),
}

# Hooks every run reaches (the rest fire only on faults, unit changes or failures)
ALWAYS = ("pre_admit", "on_job_start", "on_job_end", "pre_submit", "post_result", "pre_sink")


class PassThrough(BasePlugin):
    """Every hook, keeping every value; counts its calls and the events it receives."""

    name = "pass_through"
    events = "all"

    def __init__(self):
        self.calls = Counter()

    def hit(self, hook: str) -> None:
        self.calls[hook] += 1

    def pre_admit(self, item, **ctx):
        self.hit("pre_admit")

    def vote_admit(self, signals):
        self.hit("vote_admit")
        return True

    def on_job_start(self, job, *args, **kw):
        self.hit("on_job_start")

    def on_job_end(self, record):
        self.hit("on_job_end")

    def pre_submit(self, item, **ctx):
        self.hit("pre_submit")

    def post_result(self, result, **ctx):
        self.hit("post_result")

    def on_unit_state(self, unit, state):
        self.hit("on_unit_state")

    def on_fault(self, info):
        self.hit("on_fault")

    def pre_sink(self, record):
        self.hit("pre_sink")

    def on_machine_start(self):
        self.hit("on_machine_start")

    def on_machine_stop(self):
        self.hit("on_machine_stop")

    def on_event(self, event):
        self.hit("event")


def rotation(seed: int) -> list[str]:
    """Selectors of the call, batch and stream lanes: over five seeds every lane runs each."""
    names = list(SELECTORS)
    return [names[(seed + k) % len(names)] for k in range(3)]


def check(seed: int, name: str) -> None:
    config = dict(CONFIGS[name])
    plugin = PassThrough() if config.pop("plugged", False) else None
    if config.get("select") == "rotate":
        config["select"] = rotation(seed)
    if config.get("store_mode") is not None:
        config["store"] = MemoryStore()
    faults = "item_error_rate" in config

    p = entry_program(seed, jobs=JOBS)
    jobs = keyed(p.inputs, f"{seed}-")
    inputs_of = {j.key: j.inputs for j in jobs}
    m = build(p.text, p.entry, seed=seed, plugins=[plugin] if plugin else [], **config)
    try:
        sink = bounded(m, lambda: run_jobs(m, jobs), BOUND_S)
        assert sorted(r.key for r in sink.records) == sorted(inputs_of)
        conform_all(m, sink.records, inputs_of, mode="reexecute", pure=pure, engine_errors=faults)
        stats = m.inspect("stats").result()
        failovers = sum(lane.stats["failovers"] for lane in m.lanes)
    finally:
        m.stop(drain=False, timeout=5)

    # Injected outages took units down and their work went elsewhere
    if faults:
        assert failovers > 0

    # The store served the run
    if "store" in config:
        assert stats["store_hits"] + stats["store_misses"] > 0

    # The pass-through plugin saw every job through its hooks
    if plugin is not None:
        missed = [h for h in ALWAYS if plugin.calls[h] == 0]
        assert not missed, f"hooks never called: {missed}"
        assert plugin.calls["on_job_end"] == len(jobs)


@pytest.mark.parametrize("config", list(CONFIGS))
@pytest.mark.parametrize("seed", SEEDS)
def test_generated(seed, config):
    check(seed, config)
