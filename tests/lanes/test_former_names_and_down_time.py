"""A former naming repeated or foreign items is contained; a lane's time with no unit up counts
while it lasts."""

import logging
import time

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.policies import SizeOrWait
from togeari.testing import SimBatch, SimCall

logging.disable(logging.CRITICAL)

LIB = NodeLibrary("formernames")
STEP = "@def (x: int)main(y):\n    (x)step(y)\n"
FOREIGN = object()


@LIB.node()
def step(x: int) -> int:
    return x + 1


class NamesTwice(SizeOrWait):
    """Names the head item twice and an item that is not queued."""

    def take(self, bucket, unit, now):
        items = bucket.items()[:2]
        return [*items, *items[:1], FOREIGN] if items else 0


def test_a_former_naming_repeated_or_foreign_items_takes_each_queued_item_once():
    lane = Lane([SimBatch(name="b", latency_ms=5, tail=0.0, max_batch=4)], former=NamesTwice(4, 1))
    m = Machine(
        program=STEP,
        entry="main",
        libraries=[LIB],
        bind={"step": Use(lane), "kir.std.*": Inline()},
    )
    try:
        recs = [h.result(10) for h in [m.submit({"x": i}) for i in range(10)]]
        assert m.dispatcher.dead is None
    finally:
        m.stop()
    assert [r.outputs for r in recs] == [{"y": i + 1} for i in range(10)]
    assert lane.stats["submitted"] == 10
    assert lane.stats["policy_errors"] >= 1


def test_no_unit_seconds_grow_while_the_lane_stays_down():
    unit = SimCall(name="u", latency_ms=1, tail=0.0)
    lane = Lane([unit])
    t0 = time.monotonic()

    # The lane's only unit goes down and stays down
    lane.unit_down(unit, RuntimeError("down"), t0)
    assert lane.pump(t0) == 0
    lane.pump(t0 + 1.0)
    assert abs(lane.stats["no_unit_s"] - 1.0) < 1e-9
    lane.pump(t0 + 2.5)
    assert abs(lane.stats["no_unit_s"] - 2.5) < 1e-9
