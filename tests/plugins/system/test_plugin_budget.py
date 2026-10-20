"""Budget: caps on admitted jobs, calls per node and walltime."""

from plugin_programs import PROGRAM, jobs

from conftest import build, run_jobs
from togeari.plugins import Budget


def blocked(records) -> list:
    return [r for r in records if not r.ok and r.failure["kind"] == "Blocked"]


def test_budget_caps_admitted_jobs():
    budget = Budget(max_jobs=5)
    m = build(PROGRAM, "main", plugins=[budget])
    try:
        sink = run_jobs(m, jobs(12))
        status = m.inspect("budget").result()
    finally:
        m.stop()
    assert len(sink.records) == 12
    assert budget.jobs == 5
    assert len(blocked(sink.records)) == 7
    assert all(r.ok for r in sink.records if r not in blocked(sink.records))
    assert status["blocked"]["admit"] == 7 and status["exhausted"]


def test_budget_caps_calls_per_node():
    budget = Budget(max_calls_per_node={"inc": 3})
    m = build(PROGRAM, "main", plugins=[budget])
    try:
        sink = run_jobs(m, jobs(10))
        submitted = m.dispatcher.stats
    finally:
        m.stop()
    assert len(sink.records) == 10
    assert budget.calls["inc"] == 3
    failed = blocked(sink.records)
    assert failed and all(r.failure["node"] == "inc" for r in failed)
    assert budget.blocked["submit"] >= len(failed)
    assert submitted["jobs_ok"] + submitted["jobs_failed"] == 10


def test_budget_walltime_rejects_after_the_limit():
    m = build(PROGRAM, "main", plugins=[Budget(walltime_s=0.0)])
    try:
        sink = run_jobs(m, jobs(4))
    finally:
        m.stop()
    assert len(blocked(sink.records)) == 4
