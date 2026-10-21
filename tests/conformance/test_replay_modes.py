"""Conformance replay modes (spec/07 §7.1): `replay` follows the recorded results, `reexecute`
runs pure nodes again, so a result delivered to the wrong statement or job is caught."""

from types import SimpleNamespace

import pytest
from kohakunode import NodeLibrary

from conftest import bounded
from togeari import Inline, Lane, Machine, SourceItem, Use
from togeari.conformance import conform, replay
from togeari.core.construct.errors import Problems
from togeari.core.contracts.digest import digest
from togeari.core.program.job import build_job_programs
from togeari.io import Iterable, Memory
from togeari.testing import SimCall

LIB = NodeLibrary("conf")


@LIB.node()
def inc(x: int) -> int:
    return x + 1


@LIB.node()
def odd(x: int) -> int:
    if x % 2:
        raise ValueError(f"odd {x}")
    return x


@LIB.node()
def mklist(x: int) -> list:
    return [x]


@LIB.node(mutates=["xs"], outputs=["n"])
def push(xs: list) -> int:
    xs.append(99)
    return len(xs)


TWO = "@def (x: int)main(y):\n    (x)inc(a)\n    (a)inc(y)\n"
ONE = "@def (x: int)main(y):\n    (x)inc(y)\n"
ODD = "@def (x: int)main(y):\n    (x)odd(y)\n"
PUSH = "@def (x: int)main(n, xs):\n    (x)mklist(xs)\n    (xs)push(n)\n"
PURE = ("inc", "odd", "mklist")


def job_program(text: str):
    problems = Problems()
    jobs = build_job_programs(text, ["main"], [LIB], None, None, problems)
    problems.raise_if_any()
    return jobs[0]


def call(node: str, inputs: dict, kind: str, value) -> tuple:
    return (node, digest(inputs).hex(), kind, value, None)


def job(record: list, ok: bool = True, outputs=None, failure=None):
    return SimpleNamespace(record=record, ok=ok, outputs=outputs, failure=failure)


# ---------------------------------------------------------------- a mis-routed result
def test_reexecute_catches_results_swapped_between_jobs_replay_does_not():
    lane = Lane([SimCall(name="c", latency_ms=1, capacity=8)])
    m = Machine(program=TWO, entry="main", libraries=[LIB], bind={"inc": Use(lane)}, record=1.0)
    real = lane.results
    held = []

    # The first two results (two different jobs' first calls) trade places
    def results(sub, res, now):
        if len(held) < 2:
            held.append((sub, res))
            if len(held) < 2:
                return []
            (s1, r1), (s2, r2) = held
            return real(s1, r2, now) + real(s2, r1, now)
        return real(sub, res, now)

    lane.results = results
    jobs = [SourceItem(str(k), {"x": 10 * k}) for k in range(4)]
    inputs = {j.key: j.inputs for j in jobs}
    sink = Memory()
    try:
        bounded(m, lambda: m.run(Iterable(jobs), sink), 30)
    finally:
        m.stop(drain=False, timeout=5)
    jp = m.code.entries["main"].region.job
    recs = {r.key: r for r in sink.records}

    # Two jobs got another job's value; replay follows the record, reexecute does not
    wrong = [k for k, r in recs.items() if r.outputs["y"] != inputs[k]["x"] + 2]
    assert len(wrong) == 2
    for k in wrong:
        assert conform(jp, recs[k], inputs[k]).ok
        v = conform(jp, recs[k], inputs[k], mode="reexecute", pure=PURE)
        assert not v.ok and "differs" in v.reason, v.reason
    for k in set(recs) - set(wrong):
        assert conform(jp, recs[k], inputs[k], mode="reexecute", pure=PURE).ok


def test_a_record_with_a_foreign_result():
    jp = job_program(ONE)
    rec = job([call("inc", {"x": 1}, "ok", 5)], outputs={"y": 5})
    assert conform(jp, rec, {"x": 1}).ok
    v = conform(jp, rec, {"x": 1}, mode="reexecute", pure=PURE)
    assert not v.ok and "call 1 (inc)" in v.reason

    # Not marked pure: replayed
    assert conform(jp, rec, {"x": 1}, mode="reexecute", pure=["odd"]).ok
    assert conform(jp, rec, {"x": 1}, mode="reexecute", pure=lambda name: False).ok


def test_array_outputs_are_compared_by_value():
    # Array outputs are compared by digest, never by `!=`
    np = pytest.importorskip("numpy")
    jp = job_program(ONE)
    out = np.arange(3)
    rec = job([call("inc", {"x": 1}, "ok", out)], outputs={"y": np.arange(3)})
    assert conform(jp, rec, {"x": 1}).ok
    rec = job([call("inc", {"x": 1}, "ok", out)], outputs={"y": np.arange(1, 4)})
    v = conform(jp, rec, {"x": 1})
    assert not v.ok and v.reason == "outputs differ"


# ---------------------------------------------------------------- failures
def test_a_result_where_the_node_fails_is_a_mismatch():
    jp = job_program(ODD)
    rec = job([call("odd", {"x": 3}, "ok", 3)], outputs={"y": 3})
    assert conform(jp, rec, {"x": 3}).ok
    v = conform(jp, rec, {"x": 3}, mode="reexecute", pure=PURE)
    assert not v.ok and "node failed with ValueError" in v.reason


def test_a_failure_the_node_also_raises_conforms():
    jp = job_program(ODD)
    err = {"kind": "ValueError", "node": "odd", "message": "odd 3"}
    rec = job(
        [call("odd", {"x": 3}, "error", err)],
        ok=False,
        failure={"kind": "ValueError", "node": "odd"},
    )
    for engine_errors in (True, False):
        v = conform(jp, rec, {"x": 3}, mode="reexecute", pure=PURE, engine_errors=engine_errors)
        assert v.ok and v.details == [], v


def test_an_engine_failure_of_a_pure_node_is_kept_only_when_allowed():
    jp = job_program(ODD)
    err = {"kind": "SimError", "node": "odd", "message": "injected"}
    rec = job(
        [call("odd", {"x": 2}, "error", err)], ok=False, failure={"kind": "SimError", "node": "odd"}
    )
    v = conform(jp, rec, {"x": 2}, mode="reexecute", pure=PURE)
    assert v.ok and len(v.details) == 1 and "engine failure" in v.details[0]
    v = conform(jp, rec, {"x": 2}, mode="reexecute", pure=PURE, engine_errors=False)
    assert not v.ok and "node ok" in v.reason

    # The node raises another kind
    err3 = {"kind": "SimError", "node": "odd", "message": "injected"}
    rec3 = job(
        [call("odd", {"x": 3}, "error", err3)],
        ok=False,
        failure={"kind": "SimError", "node": "odd"},
    )
    assert conform(jp, rec3, {"x": 3}, mode="reexecute", pure=PURE).ok
    v = conform(jp, rec3, {"x": 3}, mode="reexecute", pure=PURE, engine_errors=False)
    assert not v.ok and "node with ValueError" in v.reason


# ---------------------------------------------------------------- records stay as recorded
@pytest.mark.parametrize("mode", ["replay", "reexecute"])
def test_conform_never_changes_the_record(mode):
    m = Machine(
        program=PUSH,
        entry="main",
        libraries=[LIB],
        bind={"mklist": Use(Lane([SimCall(name="s", latency_ms=1)])), "push": Inline()},
        record=1.0,
    )
    try:
        rec = bounded(m, lambda: m.submit({"x": 7}).result(10), 15)
    finally:
        m.stop(drain=False, timeout=5)
    jp = m.code.entries["main"].region.job
    before = digest(rec.record)
    for _ in range(2):
        v = conform(jp, rec, {"x": 7}, mode=mode, pure=PURE)
        assert v.ok, v.reason
    assert digest(rec.record) == before
    assert rec.record[0][3] == [7]


def test_reexecute_compares_an_in_place_change_of_a_node_marked_pure():
    jp = job_program(PUSH)
    rec = [call("mklist", {"x": 7}, "ok", [7]), call("push", {"xs": [7]}, "ok", 2)]
    rec[1] = (*rec[1][:4], {"xs": [7, 99]})
    good = job(rec, outputs={"n": 2, "xs": [7, 99]})
    assert conform(jp, good, {"x": 7}, mode="reexecute", pure=(*PURE, "push")).ok
    rec[1] = (*rec[1][:4], {"xs": [7, 98]})
    bad = job(rec, outputs={"n": 2, "xs": [7, 98]})
    assert conform(jp, bad, {"x": 7}).ok
    v = conform(jp, bad, {"x": 7}, mode="reexecute", pure=(*PURE, "push"))
    assert not v.ok and "change of 'xs'" in v.reason


# ---------------------------------------------------------------- arguments
def test_arguments_are_checked():
    jp = job_program(ONE)
    rec = [call("inc", {"x": 1}, "ok", 2)]
    with pytest.raises(ValueError, match="mode"):
        replay(jp, rec, {"x": 1}, mode="rerun")
    with pytest.raises(TypeError, match="pure"):
        replay(jp, rec, {"x": 1}, mode="reexecute", pure="inc")
    assert replay(jp, rec, {"x": 1}, mode="reexecute", pure=PURE) == ("ok", {"y": 2}, [])
