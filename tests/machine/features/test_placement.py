"""Unit placement through the machine: `ByHint` sends hinted calls to the named unit, `Affinity`
keeps each job on one unit."""

from collections import defaultdict

from feature_nodes import LIB

from conftest import bounded
from togeari import Lane, Machine, SourceItem, Use
from togeari.io import Iterable, Memory
from togeari.policies import Affinity, ByHint
from togeari.testing.sim import SimEngine

HINTED = '@def (x: int)main(y):\n    @hint unit="u1"\n    (x)double(a)\n    (a)step(y)\n'
THREE = "@def (x: int)main(y):\n    (x)step(a)\n    (a)step(b)\n    (b)step(y)\n"


class Seen(SimEngine):
    """A sim call unit that notes the job key of every item it takes."""

    def __init__(self, name: str, **kw):
        super().__init__("call", name=name, **kw)
        self.jobs: list = []

    def submit(self, work, done):
        items = work if isinstance(work, list) else [work]
        self.jobs += [(it.node, it.ext.get("job_key")) for it in items]
        return super().submit(work, done)


def run(m, n: int) -> list:
    sink = Memory()
    jobs = [SourceItem(f"j{i}", {"x": i}) for i in range(n)]
    bounded(m, lambda: m.run(Iterable(jobs), sink), 30)
    return sink.records


def test_by_hint_places_hinted_calls_on_the_named_unit():
    units = [Seen(f"u{i}", latency_ms=1, capacity=4) for i in range(3)]
    lane = Lane(units, select=ByHint())
    m = Machine(
        program=HINTED, entry="main", libraries=[LIB], bind={"double": Use(lane), "step": Use(lane)}
    )
    try:
        recs = run(m, 24)
    finally:
        m.stop(drain=False, timeout=5)
    assert sorted(r.outputs["y"] for r in recs) == sorted(2 * i + 1 for i in range(24))

    # Every hinted call ran on u1; the unhinted ones spread
    hinted = {u.name: sum(node == "double" for node, _ in u.jobs) for u in units}
    assert hinted == {"u0": 0, "u1": 24, "u2": 0}
    assert sum(node == "step" for u in units for node, _ in u.jobs) == 24


def test_affinity_keeps_every_job_on_one_unit():
    units = [Seen(f"a{i}", latency_ms=1, capacity=64) for i in range(3)]
    lane = Lane(units, select=Affinity())
    m = Machine(program=THREE, entry="main", libraries=[LIB], bind={"step": Use(lane)})
    try:
        recs = run(m, 30)
    finally:
        m.stop(drain=False, timeout=5)
    assert sorted(r.outputs["y"] for r in recs) == [i + 3 for i in range(30)]

    # A job's three calls share a unit; the jobs spread over the units
    where = defaultdict(set)
    for u in units:
        for _, key in u.jobs:
            where[key].add(u.name)
    assert len(where) == 30 and all(len(names) == 1 for names in where.values()), where
    assert len({next(iter(names)) for names in where.values()}) > 1
