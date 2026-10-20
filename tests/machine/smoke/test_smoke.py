"""A first end-to-end run: a small program on simulated engines, checked against the reference."""

from conftest import build, pure, run_jobs
from togeari.conformance import conform

PROGRAM = """
@def (a: int, b: int = 3)main(result):
    (a)inc(x)
    (x, b)mix(y)
    (y)small(c)
    (c)branch(`yes`, `no`)
    yes:
        (y)inc(z)
    no:
        z = 7
    (z, x)mix(result)
"""


def test_smoke_matches_reference():
    m = build(PROGRAM, "main")
    try:
        sink = run_jobs(m, [{"a": i} for i in range(20)])
        jp = m.code.entries["main"].region.job
        assert len(sink.records) == 20
        for rec in sink.records:
            assert rec.ok, (rec.key, rec.failure)

            # Every node runs again in the reference; a recorded failure is a mismatch
            v = conform(
                jp, rec, {"a": int(rec.key)}, mode="reexecute", pure=pure, engine_errors=False
            )
            assert v.ok, (rec.key, v)
    finally:
        m.stop()
