"""The hang detector of the machine checks: a run that never ends fails with the machine's dump."""

import pytest
from kohakunode import NodeLibrary

from conftest import bounded, run_jobs
from togeari import EngineBase, Lane, Machine, Use

LIB = NodeLibrary("hang")


@LIB.node()
def stuck(x: int) -> int:
    return x


class Silent(EngineBase):
    """Takes every item and never answers."""

    def capacity(self):
        return 8

    def submit(self, work, done):
        return 1


def test_a_run_that_never_ends_fails_with_the_resident_jobs():
    m = Machine(
        program="@def (x: int)main(y):\n    (x)stuck(y)\n",
        entry="main",
        libraries=[LIB],
        bind={"stuck": Use(Lane([Silent(name="silent")], lane_dead_s=None))},
    )
    try:
        with pytest.raises(pytest.fail.Exception) as e:
            bounded(m, lambda: run_jobs(m, [{"x": 1}, {"x": 2}]), 1.0)
    finally:
        m.stop(drain=False, timeout=5)
    text = str(e.value)
    assert "hang" in text and "'entry': 'main'" in text and "lanes:" in text
