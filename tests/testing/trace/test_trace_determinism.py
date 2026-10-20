"""The trace driver and decision log (spec/11 §11.5): one seed gives one trace in any process,
other seeds give other schedules with the same results, and any reordered decision shows."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from kohakunode.testing import entry_program
from trace_child import digests
from trace_machines import TINY, TWO, generated, tiny

from conftest import conform_all, pure
from togeari import Lane, Machine, Use
from togeari.policies import DeepestFirst
from togeari.testing import SimCall
from togeari.testing.trace import (
    Decision,
    DecisionLog,
    ScriptedEngine,
    TraceStuck,
    first_difference,
    run_trace,
)

HERE = Path(__file__).resolve().parent
GEN_SEEDS = range(8)

# Digest of the decision log of `tiny()` over jobs x=1 and x=5 at trace seed 0
TINY_LOG = "caea612daa73e473e7e6f06905c1a9e72ef617f295a068748a0e36dc0ed2b038"


def trace(gen_seed: int, seed: int, **kw):
    """A trace of 16 generated jobs; item failures and an outage of unit 0 by default."""
    p = entry_program(gen_seed, jobs=16)
    kw.setdefault("item_error_rate", 0.05)
    kw.setdefault("outages", [(0.004, 0.02)])
    return run_trace(generated(p.text, p.entry, **kw), p.inputs, seed=seed), p


def outputs(records) -> list:
    return sorted((r.key, r.ok, repr(r.outputs)) for r in records)


def sim_machine() -> Machine:
    lane = Lane([SimCall(name="sim", latency_ms=0.0)], name="calls")
    return Machine(program=TWO, entry="main", libraries=[TINY], bind={"inc": Use(lane)})


# ------------------------------------------------------------ the log's content
def test_one_job_makes_the_decisions_of_the_protocol():
    """Both calls fetch at once; the second issues after the first's writeback; retire in order."""
    t = run_trace(tiny(), [{"x": 1}], seed=0)
    first, second = (0,), (1,)
    assert [(d.kind, d.key, d.unit) for d in t.log] == [
        ("admit", (), None),
        ("fetch", first, None),
        ("fetch", second, None),
        ("issue", first, None),
        ("submit", first, "u"),
        ("writeback", first, "u"),
        ("issue", second, None),
        ("submit", second, "u"),
        ("retire", first, None),
        ("writeback", second, "u"),
        ("retire", second, None),
        ("end", (), "ok"),
    ]
    assert {d.job for d in t.log} == {"0"}
    assert {d.lane for d in t.log if d.kind not in ("admit", "end")} == {"calls"}
    assert [(r.key, r.ok, r.outputs) for r in t.records] == [("0", True, {"y": 3})]


def test_the_tiny_trace_is_pinned():
    t = run_trace(tiny(), [{"x": 1}, {"x": 5}], seed=0)
    assert t.log.digest() == TINY_LOG


# ------------------------------------------------------------ determinism
@pytest.mark.parametrize("gen_seed", GEN_SEEDS)
def test_one_seed_gives_one_trace(gen_seed):
    a, _ = trace(gen_seed, seed=7)
    b, _ = trace(gen_seed, seed=7)
    assert a.log.first_mismatch(b.log) is None
    assert first_difference(a.records, b.records) is None
    assert (a.cycles, a.clock_s) == (b.cycles, b.clock_s)


@pytest.mark.parametrize("gen_seed", [0, 3])
def test_one_seed_gives_one_trace_in_a_fresh_process(gen_seed):
    """A child process with another string-hash seed prints the same digests."""
    here = digests(gen_seed, 7)
    env = {**os.environ, "PYTHONHASHSEED": str(1000 + gen_seed)}
    env["PYTHONPATH"] = os.pathsep.join([str(HERE), *filter(None, [env.get("PYTHONPATH")])])
    out = subprocess.run(
        [sys.executable, str(HERE / "trace_child.py"), str(gen_seed), "7"],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=True,
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == here


def test_other_seeds_give_other_schedules_and_the_same_results():
    """Engine answers come in another order: the decisions differ, the outputs do not.

    A program whose jobs make one batch submission between them has one order only, so the
    decisions must differ for most generated programs, not for each.
    """
    differ = []
    for gen_seed in GEN_SEEDS:
        a, _ = trace(gen_seed, seed=1, item_error_rate=0.0, outages=None)
        b, _ = trace(gen_seed, seed=2, item_error_rate=0.0, outages=None)
        assert outputs(a.records) == outputs(b.records), gen_seed
        if a.log.first_mismatch(b.log) is not None:
            differ.append(gen_seed)
    assert len(differ) >= len(GEN_SEEDS) - 2, differ


@pytest.mark.parametrize("gen_seed", GEN_SEEDS)
def test_traced_records_conform(gen_seed):
    t, p = trace(gen_seed, seed=3, item_error_rate=0.0, outages=None)
    inputs_of = {str(i): x for i, x in enumerate(p.inputs)}
    assert sorted(r.key for r in t.records) == sorted(inputs_of)
    conform_all(t.machine, t.records, inputs_of, mode="reexecute", pure=pure, engine_errors=False)


# ------------------------------------------------------------ detecting differences
def test_a_swapped_decision_is_found_at_its_place():
    t, _ = trace(0, seed=7)
    log = t.log.decisions
    i = next(k for k in range(10, len(log) - 1) if log[k] != log[k + 1])
    swapped = DecisionLog([*log[:i], log[i + 1], log[i], *log[i + 2 :]])
    miss = t.log.first_mismatch(swapped)
    assert miss is not None and miss.index == i
    assert (miss.mine, miss.other) == (log[i], log[i + 1])
    assert swapped.digest() != t.log.digest()


def test_a_dropped_or_added_decision_is_found():
    t = run_trace(tiny(), [{"x": 1}], seed=0)
    log = t.log.decisions
    assert t.log.first_mismatch(DecisionLog(log[:-1])).index == len(log) - 1
    extra = Decision("retire", "0", None, None, (9,), None, None)
    assert t.log.first_mismatch(DecisionLog([*log, extra])).index == len(log)


def test_another_issue_order_shows_in_the_log():
    """The same seed under DeepestFirst: other decisions, the same outputs."""
    p = entry_program(0, jobs=16)
    base = run_trace(generated(p.text, p.entry), p.inputs, seed=5)
    deep = run_trace(generated(p.text, p.entry, issue=DeepestFirst()), p.inputs, seed=5)
    assert base.log.first_mismatch(deep.log) is not None
    assert outputs(base.records) == outputs(deep.records)


def test_the_log_file_reads_back_as_written(tmp_path):
    t, _ = trace(1, seed=7)
    path = tmp_path / "log.jsonl"
    t.log.write(path)
    back = DecisionLog.read(path)
    assert back == t.log and back.digest() == t.log.digest()


# ------------------------------------------------------------ refusals and limits
def test_only_scripted_engines_are_driven():
    with pytest.raises(TypeError, match="SimEngine"):
        run_trace(sim_machine, [{"x": 1}], seed=0)


def test_a_scripted_engine_refuses_work_outside_a_trace():
    with pytest.raises(RuntimeError, match="trace driver"):
        ScriptedEngine("call", name="u").submit([], lambda *a: None)


def test_a_run_past_its_cycle_cap_is_reported():
    with pytest.raises(TraceStuck, match="3 cycles"):
        run_trace(tiny(), [{"x": 1}], seed=0, max_cycles=3)
