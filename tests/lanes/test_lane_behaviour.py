"""Lane behaviour: no busy deadlines, early forming, elastic units, identity, queue costs,
retry backoff, the Selector contract, the dead lane, wake times, policies, sim RNGs."""

import time

import pytest
from kohakunode import NodeLibrary

from conftest import CountOutage
from togeari import Machine, Use, policies
from togeari.core.contracts import Selector as ContractSelector
from togeari.core.contracts.policies import Former
from togeari.core.contracts.results import Ok, UnitError
from togeari.core.contracts.views import ReadyView
from togeari.core.contracts.workitem import WorkItem
from togeari.core.lanes.lane import Lane, Retry, State
from togeari.core.policies import select as select_mod
from togeari.policies import Affinity, LaneAware, Selector, SizeOrWait
from togeari.testing import SimBatch
from togeari.testing.sim import SimEngine

LIB = NodeLibrary("lanes_behaviour")


@LIB.node()
def inc(x: int) -> int:
    return x + 1


class Load:
    waiting = 0


class Unit:
    """A unit without threads: records submits; capacity, readiness and wake time are set."""

    def __init__(self, name="u", kind="call", cap=1, max_batch=8, keys=1, wake=None):
        self.name, self.kind, self.cap, self.max_batch = name, kind, cap, max_batch
        self.keys = keys
        self.wake = wake
        self.ok = True
        self.subs: list = []
        self.cancelled: list = []
        self.spec_calls = 0
        self.key_calls = 0

    def capacity(self):
        return self.cap

    def load(self):
        return Load()

    def ready(self):
        return self.ok

    def spec(self):
        self.spec_calls += 1
        return {"engine": "fake", "config": self.keys, "name": self.name}

    def key(self, item):
        self.key_calls += 1
        return item.inputs["x"] % self.keys

    def submit(self, work, done):
        self.subs.append(work)
        return len(self.subs)

    def cancel(self, ticket):
        self.cancelled.append(ticket)


def wake_unit(**kw):
    u = Unit(**kw)
    u.wake_at = lambda: u.wake
    return u


def item(x, priority=0, hint=None):
    it = WorkItem("f", {"x": x}, None, priority=priority)
    it.ext["hint"] = hint or {}
    return it


def wire(lane):
    """Capture done callbacks and events like the machine does."""
    done, events = [], []
    lane.on_done = lambda ln, sub, results: done.append((sub, results))
    lane.emit = lambda kind, **f: events.append((kind, f))
    return done, events


def open_subs(lane):
    return list(lane.subs.values())


# ---------------------------------------------------------------- no busy deadline
def test_expired_former_wait_with_full_units_is_not_a_past_deadline():
    # A full unit makes no deadline out of an expired former wait
    unit = Unit(kind="batch", cap=0)
    lane = Lane([unit], former=SizeOrWait(32, 1))
    wire(lane)
    now = time.monotonic()
    for i in range(4):
        lane.enqueue(item(i), now - 1.0)
    lane.pump(now)
    d = lane.next_deadline(now + 0.001)
    assert d is None or d > now + 0.001


def test_completion_makes_the_next_pump_due():
    unit = Unit(kind="batch", cap=1)
    lane = Lane([unit], former=SizeOrWait(2, 1))
    wire(lane)
    now = time.monotonic()
    for i in range(6):
        lane.enqueue(item(i), now)
    lane.pump(now)
    unit.cap = 0
    assert lane.next_deadline(now) is None or lane.next_deadline(now) > now
    sub = open_subs(lane)[0]
    lane.results(sub, [Ok(1)] * len(sub.items), now)
    assert lane.next_deadline(now) == now


def test_hinted_wait_in_a_younger_bucket_is_served():
    # A hinted wait in any bucket, not only the oldest item's, makes that bucket takeable
    unit = Unit(kind="batch", cap=4, keys=2)
    lane = Lane([unit], former=SizeOrWait(32, 1000))
    wire(lane)
    now = time.monotonic()
    lane.enqueue(item(0), now - 0.5)
    lane.enqueue(item(1, hint={"max_wait_ms": 1}), now - 0.01)
    assert lane.next_deadline(now - 0.01) <= now
    lane.pump(now)
    assert [[it.inputs["x"] for it in work] for work in unit.subs] == [[1]]
    d = lane.next_deadline(now)
    assert d is not None and d > now


def test_machine_does_not_spin_with_a_backlog_and_full_units():
    # A backlog behind full units leaves the dispatcher waiting, under 1000 cycles per second
    unit = SimBatch(name="b", latency_ms=400, capacity=1, max_batch=4)
    m = Machine(
        program="@def (x: int)main(y):\n    (x)inc(y)\n",
        entry="main",
        libraries=[LIB],
        bind={"inc": Use(Lane([unit], former=SizeOrWait(4, 1)))},
    )
    try:
        for i in range(40):
            m.submit({"x": i})
        time.sleep(0.1)
        c0 = m.dispatcher.stats["cycles"]
        time.sleep(0.3)
        rate = (m.dispatcher.stats["cycles"] - c0) / 0.3
        assert rate < 1000, rate
    finally:
        m.stop(drain=False, timeout=0.1)


# ---------------------------------------------------------------- early forming
def test_early_forming_serves_items_moved_to_the_shared_queue():
    # Items a downed unit gives back to the shared queue reach another unit
    a, b = Unit("a", kind="batch", cap=1), Unit("b", kind="batch", cap=0)
    lane = Lane([a, b], former=SizeOrWait(2, 1), forming="early", retry=Retry(3, 0))
    wire(lane)
    now = time.monotonic()
    for i in range(4):
        lane.enqueue(item(i), now)
    assert len(lane._q) == 0
    lane.pump(now)
    sub = open_subs(lane)[0]
    lane.results(sub, [UnitError(ConnectionError("x"))] * len(sub.items), now)
    assert lane.state[id(a)].state == State.DOWN
    b.cap = 4
    lane.pump(now + 0.01)
    assert sum(len(w) for w in b.subs) == 4
    assert lane.queued() == 0


def test_early_forming_items_queued_while_no_unit_up_reach_units():
    a = Unit("a", cap=0)
    lane = Lane([a], forming="early")
    wire(lane)
    now = time.monotonic()
    lane.unit_down(a, UnitError(ConnectionError("x")), now)
    for i in range(3):
        lane.enqueue(item(i), now)
    assert len(lane._q) == 3
    a.cap = 8
    lane.timers(lane.state[id(a)].next_poll)
    assert lane.state[id(a)].state == State.UP
    lane.pump(now + 1)
    assert len(a.subs) == 3


def test_machine_early_forming_with_an_outage_finishes_every_job():
    # Every job ends; the flaky unit goes down at its second submit and is back at its first
    # health poll
    flaky = CountOutage("batch", down_at=2, polls=1, name="flaky", latency_ms=5, max_batch=4)
    units = [SimBatch(name="good", latency_ms=5, max_batch=4), flaky]
    lane = Lane(units, former=SizeOrWait(4, 1), forming="early", retry=Retry(max=5))
    m = Machine(
        program="@def (x: int)main(y):\n    (x)inc(y)\n",
        entry="main",
        libraries=[LIB],
        bind={"inc": Use(lane)},
        start_timeout_s=2.0,
    )
    try:
        handles = [m.submit({"x": i}) for i in range(40)]
        results = [h.result(10) for h in handles]
        assert all(r.ok for r in results)
        assert lane.queued() == 0
        assert flaky.counts["outage_rejects"] >= 1 and lane.stats["failovers"] >= 1
    finally:
        m.stop(drain=False, timeout=0.1)


# ---------------------------------------------------------------- attach / detach
def test_reattach_does_not_duplicate_and_attached_twice_raises():
    a, b = Unit("a"), Unit("b")
    lane = Lane([a])
    _, events = wire(lane)
    lane.attach(b)
    with pytest.raises(ValueError):
        lane.attach(b)
    with pytest.raises(ValueError):
        lane.attach(Unit("a"))
    lane.detach(b, False, time.monotonic())
    assert lane.units == [a]
    lane.attach(b)
    assert lane.units == [a, b]
    assert lane.state[id(b)].state == State.UP
    assert [k for k, _ in events].count("unit.attach") == 2


def test_attach_of_a_unit_not_ready_is_down_and_polled():
    a, b = Unit("a"), Unit("b")
    b.ok = False
    lane = Lane([a])
    wire(lane)
    lane.attach(b)
    st = lane.state[id(b)]
    assert st.state == State.DOWN
    b.ok = True
    lane.timers(st.next_poll)
    assert st.state == State.UP


def test_detach_drain_reaches_detached_when_work_ends():
    # A draining unit is detached once its last submission settles
    a = Unit("a", cap=2)
    lane = Lane([a])
    _, events = wire(lane)
    now = time.monotonic()
    lane.enqueue(item(1), now)
    lane.pump(now)
    lane.detach(a, True, now)
    assert lane.state[id(a)].state == State.DRAINING
    sub = open_subs(lane)[0]
    lane.results(sub, [Ok(2)], now)
    assert lane.state[id(a)].state == State.DETACHED and lane.units == []
    assert ("unit.detached", {"unit": "a", "reason": "drained"}) in events


def test_lane_has_no_starting_path():
    # Units are up or down when attached; there is no starting state
    lane = Lane([Unit("a")])
    assert not hasattr(State, "STARTING") and not hasattr(lane, "started")
    with pytest.raises(TypeError):
        lane.attach(Unit("b"), starting=True)


# ---------------------------------------------------------------- former defaults
class TakeOnly(Former):
    """A former written with ``take`` only."""

    def take(self, bucket, unit, now):
        return 1


def test_a_former_with_only_take_serves_its_bucket():
    # The default deadline makes a non-empty bucket takeable at every pump
    unit = Unit(kind="batch", cap=1)
    lane = Lane([unit], former=TakeOnly())
    wire(lane)
    now = time.monotonic()
    lane.enqueue(item(1), now)
    lane.pump(now)
    assert len(unit.subs) == 1


def test_detach_now_requeues_unsettled_items():
    a, b = Unit("a", cap=2), Unit("b", cap=0)
    lane = Lane([a, b])
    wire(lane)
    now = time.monotonic()
    lane.enqueue(item(1), now)
    lane.pump(now)
    back = lane.detach(a, False, now)
    assert [it.inputs["x"] for it in back] == [1]
    assert lane.state[id(a)].state == State.DETACHED and lane.inflight == 0
    b.cap = 1
    lane.pump(now)
    assert len(b.subs) == 1


def test_detach_now_lets_an_ordered_item_finish_on_its_unit():
    # An ordered item stays on its unit through a detach, so its effect runs once
    a, b = Unit("a", cap=2), Unit("b", cap=1)
    lane = Lane([a, b])
    done, _ = wire(lane)
    now = time.monotonic()
    effect = item(1)
    effect.ext["ordered_once"] = True
    lane.enqueue(effect, now)
    lane.pump(now)
    sub = next(s for s in open_subs(lane) if s.unit is a)

    # Not re-queued, not cancelled; the unit drains it, then detaches
    assert lane.detach(a, False, now) == []
    assert a.cancelled == [] and lane.state[id(a)].state == State.DRAINING
    lane.pump(now)
    assert b.subs == []
    lane.results(sub, [Ok(2)], now)
    assert lane.state[id(a)].state == State.DETACHED


# ---------------------------------------------------------------- engine identity
def test_engine_identity_reads_specs_when_units_join():
    # spec() is read once per unit joining, never per identity read
    a, twin, other = Unit("a"), Unit("twin"), Unit("other", keys=2)
    lane = Lane([a])
    assert a.spec_calls == 1
    first = lane.engine_identity()
    for _ in range(100):
        assert lane.engine_identity() == first
    assert a.spec_calls == 1 and isinstance(first, bytes)

    # Same engine config under another name: unchanged
    lane.attach(twin)
    assert lane.engine_identity() == first and twin.spec_calls == 1

    # Another engine config: changed while it is in the lane
    lane.attach(other)
    second = lane.engine_identity()
    assert second != first
    lane.detach(other, False, time.monotonic())
    assert lane.engine_identity() == first
    lane.attach(other)
    assert lane.engine_identity() == second and other.spec_calls == 2


def test_engine_identity_ignores_unit_names():
    assert Lane([Unit("a")]).engine_identity() == Lane([Unit("b")]).engine_identity()
    assert Lane([Unit("a")]).engine_identity() != Lane([Unit("a", keys=2)]).engine_identity()


# ---------------------------------------------------------------- queue costs
class CountingSelector(Selector):
    def __init__(self):
        self.calls = 0

    def admits(self, item, unit, lane):
        self.calls += 1
        return True


def test_taking_an_item_does_not_scan_the_queue():
    # admits() is asked once per item taken, not once per queued item
    sel = CountingSelector()
    unit = Unit(cap=10)
    lane = Lane([unit], select=sel)
    wire(lane)
    now = time.monotonic()
    for i in range(5000):
        lane.enqueue(item(i, priority=i % 3), now)
    lane.pump(now)
    assert len(unit.subs) == 10 and sel.calls == 10
    assert [it.priority for it in unit.subs] == [2] * 10


def test_batch_key_is_read_once_per_item():
    # unit.key() is read once per item, however many pumps pass
    unit = Unit(kind="batch", cap=0, keys=4)
    lane = Lane([unit], former=SizeOrWait(8, 1000))
    wire(lane)
    now = time.monotonic()
    for i in range(400):
        lane.enqueue(item(i), now)
    for _ in range(20):
        lane.pump(now)
        lane.next_deadline(now)
    unit.cap = 5
    lane.pump(now)
    assert unit.key_calls == 400
    assert [len(w) for w in unit.subs] == [8] * 5
    assert all(len({it.inputs["x"] % 4 for it in w}) == 1 for w in unit.subs)


def test_remove_and_priority_order():
    unit = Unit(cap=0)
    lane = Lane([unit])
    wire(lane)
    now = time.monotonic()
    its = [item(i, priority=i % 2) for i in range(6)]
    for it in its:
        lane.enqueue(it, now)
    assert lane.remove(its[3]) and not lane.remove(its[3])
    assert [it.inputs["x"] for it in lane.queue] == [1, 5, 0, 2, 4]
    unit.cap = 10
    lane.pump(now)
    assert [it.inputs["x"] for it in unit.subs] == [1, 5, 0, 2, 4]


def test_affinity_hashes_a_key_once_while_units_are_unchanged(monkeypatch):
    calls = []
    real = select_mod._rendezvous
    monkeypatch.setattr(
        select_mod, "_rendezvous", lambda k, units: calls.append(k) or real(k, units)
    )
    a, b = Unit("a", cap=0), Unit("b", cap=0)
    lane = Lane([a, b], select=Affinity())
    sel = lane.select
    it = item(1)
    it.ext["job_key"] = "j1"
    for _ in range(50):
        sel.admits(it, a, lane)
        sel.admits(it, b, lane)
    assert calls == ["j1"]
    lane.unit_down(a, UnitError(ConnectionError("x")), time.monotonic())
    sel.admits(it, b, lane)
    assert calls == ["j1", "j1"]


def test_lane_aware_reads_each_lane_once():
    class Counting(Lane):
        reads = 0

        def queued(self):
            Counting.reads += 1
            return super().queued()

    lane = Counting([Unit()])

    class Binding:
        inline = False

    Binding.lane = lane

    class Instr:
        binding = Binding()

    class Inf:
        instr = Instr()

        def __init__(self, k):
            self.key = (k,)

    LaneAware().order([ReadyView(Inf(k)) for k in range(100)])
    assert Counting.reads == 1


# ---------------------------------------------------------------- retry backoff
def test_retry_waits_its_backoff_without_polling():
    # A retried item is queued again only after its backoff, as a deadline
    a, b = Unit("a", cap=1), Unit("b", cap=0)
    lane = Lane([a, b], retry=Retry(max=3, backoff_ms=100))
    wire(lane)
    now = time.monotonic()
    lane.enqueue(item(1), now)
    lane.pump(now)
    sub = open_subs(lane)[0]
    lane.results(sub, [UnitError(ConnectionError("x"))], now)
    assert lane.queued_now() == 0 and lane.queued() == 1
    assert lane.next_deadline(now) == pytest.approx(now + 0.1, abs=1e-6)
    b.cap = 1
    lane.timers(now + 0.05)
    lane.pump(now + 0.05)
    assert b.subs == []
    lane.timers(now + 0.1)
    lane.pump(now + 0.1)
    assert len(b.subs) == 1 and b.subs[0].ext["retries"] == 1


# ---------------------------------------------------------------- one Selector contract
def test_one_selector_contract():
    assert Selector is ContractSelector

    class Plain:
        """Written to the contract only: order and admits."""

        def order(self, units, lane):
            return list(reversed(units))

        def admits(self, item, unit, lane):
            return unit.name == "b"

    a, b = Unit("a", cap=5), Unit("b", cap=5)
    lane = Lane([a, b], select=Plain())
    wire(lane)
    now = time.monotonic()
    for i in range(3):
        lane.enqueue(item(i), now)
    lane.pump(now)
    assert a.subs == [] and len(b.subs) == 3
    assert isinstance(Plain(), ContractSelector)


def test_a_policy_missing_a_contract_method_is_refused_at_construction():
    # A policy without every contract method is refused when set, before any pump
    class OldStyle:
        def order(self, units, lane):
            return list(units)

        def pick_item(self, queue, unit):
            return queue[0]

    with pytest.raises(TypeError, match="admits"):
        Lane([Unit()], select=OldStyle())
    lane = Lane([Unit(kind="batch")], former="named")
    with pytest.raises(TypeError, match="deadline"):
        lane.former = OldStyle()
    lane.select = "named"


# ---------------------------------------------------------------- dead lane
def test_dead_lane_fails_queued_items_and_emits_an_event():
    # Queued items fail as UnitUnavailable with a lane.dead event
    a = Unit("a", cap=0)
    a.ok = False
    lane = Lane([a], lane_dead_s=0.5)
    _, events = wire(lane)
    now = time.monotonic()
    lane.unit_down(a, UnitError(ConnectionError("x")), now)
    lane.enqueue(item(1), now)
    lane.pump(now)
    assert lane.next_deadline(now) == pytest.approx(now + 0.5, abs=1e-6)
    out = lane.timers(now + 0.51)
    assert len(out) == 1 and type(out[0][1].error).__name__ == "UnitUnavailable"
    assert ("lane.dead", {"lane": lane.name, "items": 1}) in events
    assert lane.queued() == 0
    assert lane.next_deadline(now + 0.6) == lane.state[id(a)].next_poll


# ---------------------------------------------------------------- wake times
def test_wake_at_of_a_full_unit_is_a_deadline():
    # A full unit's future wake time is the lane's deadline; a past one is none
    now = time.monotonic()
    unit = wake_unit(cap=0, wake=now + 0.05)
    lane = Lane([unit])
    wire(lane)
    lane.enqueue(item(1), now)
    lane.pump(now)
    assert lane.next_deadline(now) == pytest.approx(now + 0.05, abs=1e-6)
    unit.wake = now - 1.0
    assert lane.next_deadline(now) is None


def test_units_without_wake_at_are_unaffected():
    now = time.monotonic()
    lane = Lane([Unit(cap=0)])
    wire(lane)
    lane.enqueue(item(1), now)
    lane.pump(now)
    assert lane.next_deadline(now) is None


# ---------------------------------------------------------------- timeouts and hints
def test_item_deadline_bounds_the_lane_timeout_in_flight():
    unit = Unit(cap=1)
    lane = Lane([unit], timeout_s=5.0)
    wire(lane)
    now = time.monotonic()
    it = item(1)
    it.deadline = now + 0.2
    lane.enqueue(it, now)
    lane.pump(now)
    assert it.deadline == pytest.approx(now + 0.2, abs=1e-6)
    assert lane.next_deadline(now) == pytest.approx(now + 0.2, abs=1e-6)
    out = lane.timers(now + 0.2)
    assert [type(r.error).__name__ for _, r in out] == ["ItemTimeout"]
    assert unit.cancelled == [1] and lane.inflight == 0


def test_item_deadline_while_queued():
    unit = Unit(cap=0)
    lane = Lane([unit])
    wire(lane)
    now = time.monotonic()
    late, other = item(1), item(2)
    late.deadline = now + 0.2
    lane.enqueue(late, now)
    lane.enqueue(other, now)
    lane.pump(now)
    assert lane.next_deadline(now) == pytest.approx(now + 0.2, abs=1e-6)
    out = lane.timers(now + 0.2)
    assert [(it, type(r.error).__name__) for it, r in out] == [(late, "ItemTimeout")]
    assert lane.queue == [other] and lane.next_deadline(now + 0.2) is None


def test_unit_without_protocol_keeps_the_item_protocol():
    unit = Unit(cap=1)
    lane = Lane([unit])
    wire(lane)
    it = item(1)
    it.protocol = "custom"
    lane.enqueue(it, 0.0)
    lane.pump(0.0)
    assert unit.subs[0].protocol == "custom"


# ---------------------------------------------------------------- policies, constants, sim
def test_speculate_is_removed():
    assert not hasattr(policies, "Speculate") and "Speculate" not in policies.__all__


def test_unit_states_are_namespace_values():
    lane = Lane([Unit()])
    match next(iter(lane.state.values())).state:
        case State.UP:
            pass
        case _:
            raise AssertionError("not up")


class SimItem:
    node = "f"

    def __init__(self, x):
        self.x = x

    def digest(self):
        return str(self.x).encode()

    def payload(self):
        return self.x

    def finish(self, raw):
        return raw


def test_sim_rngs_are_per_thread():
    # Injected failures draw from their own generator: delays are the same with or without them
    quiet = SimEngine(seed=7, item_error_rate=0.5)
    busy = SimEngine(seed=7, item_error_rate=0.5)
    plain = [quiet.delay(SimItem(i)) for i in range(20)]
    mixed = []
    for i in range(20):
        busy.run_one(SimItem(i))
        mixed.append(busy.delay(SimItem(i)))
    assert plain == mixed
