"""Generated programs across runs and programs (spec/07 §7.2): several programs in one machine
with one loaded mid-run, snapshots with a resume, a positional store rerun, effect order."""

import contextlib
import os
import time

import pytest
from kohakunode.errors import KirRuntimeError
from kohakunode.run.interpreter import Interpreter
from kohakunode.testing import EFFECTS, entry_program

from conftest import bounded, build, conform_all, keyed, pure, run_jobs
from togeari.io import Memory
from togeari.stores import MemoryStore

SEEDS = range(int(os.environ.get("TOGEARI_SEEDS", "20")))
JOBS = 32
BOUND_S = 120.0
CHECK = dict(mode="reexecute", pure=pure, engine_errors=False)


def renamed(seed: int, name: str, jobs: int):
    """A generated program whose entry is called ``name``."""
    p = entry_program(seed, jobs=jobs)
    head = ")main(result):"
    assert p.text.count(head) == 1
    p.text = p.text.replace(head, f"){name}(result):")
    p.entry = name
    return p


def mixed(groups: list[list]) -> list:
    """Jobs of several entries interleaved."""
    out = []
    for i in range(max(len(g) for g in groups)):
        out += [g[i] for g in groups if i < len(g)]
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_programs_mixed_in_one_machine_and_one_loaded_mid_run(seed):
    progs = [renamed(seed * 4 + k, f"g{k}", JOBS // 4) for k in range(4)]
    first, late = progs[:3], progs[3]
    groups = [keyed(p.inputs, f"{p.entry}-", p.entry) for p in progs]
    early_jobs, late_jobs = mixed(groups[:3]), groups[3]
    inputs_of = {j.key: j.inputs for g in groups for j in g}
    m = build(programs={p.text: [p.entry] for p in first}, seed=seed, n_units=3, window=64)

    # Half the jobs, then the fourth program loads; the rest mixed with its jobs
    def source():
        half = len(early_jobs) // 2
        yield from early_jobs[:half]
        assert m.load(late.text, [late.entry]) == [late.entry]
        yield from mixed([early_jobs[half:], late_jobs])

    try:
        sink = Memory()
        bounded(m, lambda: m.run(source(), sink), BOUND_S)
        assert sorted(r.key for r in sink.records) == sorted(inputs_of)
        assert {r.entry for r in sink.records} == {p.entry for p in progs}
        conform_all(m, sink.records, inputs_of, **CHECK)
    finally:
        m.stop(drain=False, timeout=5)


@pytest.mark.parametrize("seed", SEEDS)
def test_snapshots_resume_and_conform(seed):
    p = entry_program(seed, jobs=JOBS)
    jobs = keyed(p.inputs, f"{seed}-")
    inputs_of = {j.key: j.inputs for j in jobs}
    store = MemoryStore()

    # Run-ahead off and a narrow window: jobs reach snapshot boundaries often
    knobs = dict(seed=seed, n_units=2, window=4, run_ahead=False, store=store, snapshot_every=1)

    # First machine: stopped (jobs cancelled) once a snapshot exists or every job ended
    m = build(p.text, p.entry, latency_ms=2.0, **knobs)
    try:
        handles = [m.submit(j.inputs, key=j.key) for j in jobs]

        def first_snapshot():
            while not len(store) and not all(h.done() for h in handles):
                time.sleep(0.002)

        bounded(m, first_snapshot, BOUND_S)
    finally:
        m.stop(drain=False, timeout=5)
    snapped = len(store)

    # Second machine, same store: jobs with a snapshot resume; every job conforms
    m = build(p.text, p.entry, latency_ms=0.5, **knobs)
    try:
        sink = bounded(m, lambda: run_jobs(m, jobs), BOUND_S)
        conform_all(m, sink.records, inputs_of, **CHECK)
    finally:
        m.stop(drain=False, timeout=5)
    resumed = sum(bool(r.stats.get("resumed")) for r in sink.records)
    assert resumed > 0 or not snapped, (snapped, resumed)
    SNAPPED[seed] = snapped

    # Every job of the second run succeeded: no snapshot is left for a later rerun
    if all(r.ok for r in sink.records):
        assert len(store) == 0, f"{len(store)} snapshots left after {resumed} resumed jobs ended"


# Snapshots left by the first machine, per seed of test_snapshots_resume_and_conform
SNAPPED: dict[int, int] = {}


def test_some_seed_snapshotted_and_resumed():
    """The snapshot runs above resumed jobs for at least one seed (a resume was exercised)."""
    if not SNAPPED:
        pytest.skip("test_snapshots_resume_and_conform did not run")
    assert any(n > 0 for n in SNAPPED.values()), SNAPPED


@pytest.mark.parametrize("seed", SEEDS)
def test_positional_store_rerun_conforms(seed):
    p = entry_program(seed, jobs=JOBS)
    jobs = keyed(p.inputs, f"{seed}-")
    inputs_of = {j.key: j.inputs for j in jobs}
    store = MemoryStore()

    # The same jobs twice, each on a new machine over one store: the second run is served
    hits = []
    for _ in range(2):
        m = build(
            p.text,
            p.entry,
            seed=seed,
            n_units=2,
            window=64,
            store=store,
            store_mode="positional",
        )
        try:
            sink = bounded(m, lambda m=m: run_jobs(m, jobs), BOUND_S)
            conform_all(m, sink.records, inputs_of, **CHECK)
            hits.append(m.inspect("stats").result()["store_hits"])
        finally:
            m.stop(drain=False, timeout=5)
    assert hits[0] == 0 and hits[1] > 0, (hits, len(store))


@pytest.mark.parametrize("seed", SEEDS)
def test_ordered_effects_follow_program_order(seed):
    """`ordered` nodes run in the reference interpreter's call order, job by job."""
    p = entry_program(seed, jobs=1)
    if "emit" not in p.text:
        pytest.skip("no ordered node")
    m = build(p.text, p.entry, seed=seed, n_units=3, window=64, latency_ms=1.0)
    try:
        EFFECTS.clear()
        bounded(m, lambda: run_jobs(m, p.inputs), BOUND_S)
        mine = list(EFFECTS)
    finally:
        m.stop(drain=False, timeout=5)

    # The reference runs the same job on its own, every node for real
    jp = m.code.entries[p.entry].region.job
    EFFECTS.clear()
    with contextlib.suppress(KirRuntimeError):
        Interpreter(jp.written, jp.env, step_budget=1_000_000).call(jp.entry, p.inputs[0])
    assert mine == EFFECTS
